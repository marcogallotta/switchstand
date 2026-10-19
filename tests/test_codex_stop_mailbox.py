from __future__ import annotations

import json
import os
import subprocess
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

ROOT = Path(__file__).parents[1]
HOOK = ROOT / "scripts/codex-stop-mailbox"


class MailboxServer:
    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = responses
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                owner.requests.append(json.loads(self.rfile.read(length)))
                body = json.dumps(owner.responses.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/pending"


def invoke(payload: object, endpoint: str, *, raw: str | None = None) -> dict[str, Any]:
    result = subprocess.run(
        [HOOK],
        input=json.dumps(payload) if raw is None else raw,
        text=True,
        capture_output=True,
        check=True,
        env=os.environ | {"SWITCHSTAND_CODEX_STOP_ENDPOINT": endpoint},
    )
    return json.loads(result.stdout)


def stop(*, active: bool = False) -> dict[str, object]:
    return {
        "hook_event_name": "Stop",
        "session_id": "session-1",
        "last_assistant_message": "<!-- SWITCHSTAND_YIELD:HANDOFF -->",
        "stop_hook_active": active,
    }


def test_repository_config_keeps_mailbox_and_continuity_stop_hooks() -> None:
    config = tomllib.loads((ROOT / ".codex/config.toml").read_text())
    handlers = config["hooks"]["Stop"][0]["hooks"]

    assert [handler["type"] for handler in handlers] == ["command", "command"]
    assert handlers[0]["command"].endswith('/scripts/codex-stop-mailbox"')
    assert handlers[0]["timeout"] == 5
    assert "/scripts/codex-continuity-hook\" --mode PILOT --lifetime STANDING" in (
        handlers[1]["command"]
    )
    assert handlers[1]["timeout"] == 3


def test_empty_mailbox_allows_stop_and_active_escape_skips_read() -> None:
    with MailboxServer([{
        "status": "ok", "coordinator": True, "delivery_ids": [], "has_more": False, "next_cursor": None,
    }]) as server:
        assert invoke(stop(), server.endpoint) == {}
        assert server.requests == [{"session_id": "session-1", "cursor": None}]
        assert invoke({"hook_event_name": "Stop", "stop_hook_active": True}, server.endpoint) == {}
        assert len(server.requests) == 1


def test_pending_mail_scans_every_page_and_requests_one_continuation() -> None:
    first, second = str(uuid4()), str(uuid4())
    with MailboxServer([
        {"status": "ok", "coordinator": True, "delivery_ids": [first], "has_more": True,
         "next_cursor": first},
        {"status": "ok", "coordinator": True, "delivery_ids": [second], "has_more": False,
         "next_cursor": None},
    ]) as server:
        result = invoke(stop(), server.endpoint)

    assert result["decision"] == "block"
    assert "2 pending" in result["reason"]
    assert first in result["reason"] and second in result["reason"]
    assert server.requests[-1]["cursor"] == first


@pytest.mark.parametrize("response", [
    {"status": "denied", "reason": "agent_not_registered"},
    {"status": "recovery_required", "reason": "state_unavailable"},
    {"status": "ok", "coordinator": True, "delivery_ids": "wrong", "has_more": False,
     "next_cursor": None},
])
def test_unavailable_or_malformed_mailbox_fails_open_visibly(response: dict[str, Any]) -> None:
    with MailboxServer([response]) as server:
        result = invoke(stop(), server.endpoint)

    assert result["continue"] is True
    assert "failed open" in result["systemMessage"]


def test_malformed_hook_input_fails_open_without_read() -> None:
    with MailboxServer([]) as server:
        result = invoke({}, server.endpoint)
    assert result["continue"] is True
    assert not server.requests


@pytest.mark.parametrize("message", ["Hello", "Stopped", "<!-- SWITCHSTAND_YIELD:USER_STOP -->"])
def test_conversation_and_stop_skip_mailbox(message: str) -> None:
    with MailboxServer([]) as server:
        assert invoke(stop() | {"last_assistant_message": message}, server.endpoint) == {}
        assert not server.requests


def test_worker_mailbox_never_forces_continuation() -> None:
    with MailboxServer([{
        "status": "ok", "coordinator": False, "delivery_ids": [str(uuid4())],
        "has_more": False, "next_cursor": None,
    }]) as server:
        assert invoke(stop(), server.endpoint) == {}
