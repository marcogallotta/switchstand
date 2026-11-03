import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/claude-mail-watch"


@pytest.fixture
def watcher():
    loader = importlib.machinery.SourceFileLoader("claude_mail_watch", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def edge():
    replies, requests = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            code, body = replies.pop(0)
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, replies, requests
    server.shutdown()
    server.server_close()
    thread.join()


def summary(count=0):
    return {"status": "ok", "count": count,
            "senders": [] if not count else [{"name": "Reviewer", "count": count}]}


def test_idle_waits_60_seconds_then_exits_without_consuming(watcher, edge, monkeypatch, capsys):
    port, replies, requests = edge
    replies.extend([(200, summary()), (200, summary()), (200, summary(2))])
    sleeps = []
    monkeypatch.setattr(watcher.time, "sleep", sleeps.append)
    # A system proxy must never redirect local mailbox reads.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    assert watcher.watch("Claude + coordinator", port) == 0
    assert sleeps == [60, 60]
    assert len(requests) == 3
    for request in requests:
        parsed = urlsplit(request)
        assert parsed.path == "/internal/claude-mail-summary"
        assert parse_qs(parsed.query) == {"name": ["Claude + coordinator"]}
    output = capsys.readouterr()
    assert "2 unacknowledged (2 from Reviewer)" in output.out
    assert "acknowledged nothing" in output.out and not output.err


@pytest.mark.parametrize("code,body", [
    (401, {}), (403, {}), (503, {}), (200, b"not json"),
    (200, {"status": "denied"}), (200, summary(-1)),
    (200, {"status": "ok", "count": 1, "senders": []}),
    (200, {"status": "ok", "count": True, "senders": []}),
    (200, {"status": "ok", "count": 1, "senders": [{"name": "A", "count": "1"}]}),
])
def test_http_and_malformed_summary_fail_clearly(watcher, edge, capsys, code, body):
    port, replies, requests = edge
    replies.append((code, body))
    assert watcher.watch("Claude", port) == 1
    output = capsys.readouterr()
    assert not output.out and "claude-mail-watch:" in output.err
    assert len(requests) == 1


def test_cli_transport_failure_and_bad_arguments(edge, monkeypatch):
    port, replies, _ = edge
    monkeypatch.setenv("SWITCHSTAND_MCP_BIND_PORT", str(port))
    replies.append((200, summary(1)))
    ready = subprocess.run([str(SCRIPT), "Claude"], capture_output=True, text=True, check=False)
    assert ready.returncode == 0 and "1 unacknowledged" in ready.stdout
    monkeypatch.setenv("SWITCHSTAND_MCP_BIND_PORT", "0")
    invalid = subprocess.run([sys.executable, str(SCRIPT), "Claude"], capture_output=True, text=True, check=False)
    assert invalid.returncode == 2 and "valid port" in invalid.stderr
    missing = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False)
    assert missing.returncode == 2 and "name" in missing.stderr
    monkeypatch.setenv("SWITCHSTAND_MCP_BIND_PORT", "1")
    failed = subprocess.run([sys.executable, str(SCRIPT), "Claude"], capture_output=True, text=True, check=False)
    assert failed.returncode == 1 and "transport/summary error" in failed.stderr
