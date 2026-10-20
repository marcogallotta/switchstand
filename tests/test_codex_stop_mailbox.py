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


@pytest.fixture(autouse=True)
def opted_in_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".config/switchstand/codex-stop-head.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"session_id": "session-1"}))


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
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )

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
def test_conversation_without_pending_delivery_and_stop_release(message: str) -> None:
    with MailboxServer([{
        "status": "ok", "coordinator": True, "delivery_ids": [],
        "has_more": False, "next_cursor": None,
    }]) as server:
        assert invoke(stop() | {"last_assistant_message": message}, server.endpoint).get("decision") != "block"
        assert len(server.requests) == (0 if "USER_STOP" in message else 1)


def test_worker_mailbox_retains_legacy_pending_continuation(tmp_path: Path) -> None:
    (tmp_path / ".config/switchstand/codex-stop-head.json").unlink()
    with MailboxServer([{
        "status": "ok", "coordinator": False, "delivery_ids": [str(uuid4())],
        "has_more": False, "next_cursor": None,
    }]) as server:
        assert invoke(stop(), server.endpoint)["decision"] == "block"


def test_markerless_conversation_with_pending_delivery_has_unknown_intent() -> None:
    delivery = str(uuid4())
    with MailboxServer([{
        "status": "ok", "coordinator": True, "delivery_ids": [delivery],
        "has_more": False, "next_cursor": None,
    }]) as server:
        payload = stop() | {"last_assistant_message": "Checkpoint only"}
        first = invoke(payload, server.endpoint)
        second = invoke(payload | {"stop_hook_active": True}, server.endpoint)
    assert first["continue"] is True
    assert "UNKNOWN yield intent" in first["systemMessage"]
    assert second == {}
    assert len(server.requests) == 1


def test_canonical_without_opt_in_retains_pending_conversation_continuation(tmp_path: Path) -> None:
    (tmp_path / ".config/switchstand/codex-stop-head.json").unlink()
    with MailboxServer([{
        "status": "ok", "coordinator": True, "delivery_ids": [str(uuid4())],
        "has_more": False, "next_cursor": None,
    }]) as server:
        assert invoke(stop() | {"last_assistant_message": "Hello"}, server.endpoint)["decision"] == "block"


@pytest.mark.parametrize("local_parent_session", [False, True])
def test_subagent_retains_legacy_pending_delivery_even_for_local_parent_session(
    tmp_path: Path, local_parent_session: bool,
) -> None:
    if not local_parent_session:
        (tmp_path / ".config/switchstand/codex-stop-head.json").unlink()
    delivery = str(uuid4())
    with MailboxServer([{
        "status": "ok", "coordinator": True, "delivery_ids": [delivery],
        "has_more": False, "next_cursor": None,
    }]) as server:
        result = invoke(stop() | {"agent_id": "child"}, server.endpoint)
        assert len(server.requests) == 1
    assert result["decision"] == "block"
    assert delivery in result["reason"]
