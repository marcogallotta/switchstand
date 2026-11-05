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


def invoke(payload: object, endpoint: str, *, raw: str | None = None,
           state_home: Path | None = None) -> dict[str, Any]:
    result = subprocess.run(
        [HOOK],
        input=json.dumps(payload) if raw is None else raw,
        text=True,
        capture_output=True,
        check=True,
        env=os.environ | {"SWITCHSTAND_CODEX_STOP_ENDPOINT": endpoint,
                          "XDG_STATE_HOME": str(state_home) if state_home else os.environ.get(
                              "XDG_STATE_HOME", str(Path.home() / ".local/state"))},
    )
    return json.loads(result.stdout)


def stop(*, active: bool = False) -> dict[str, object]:
    return {
        "hook_event_name": "Stop",
        "session_id": "session-1",
        "stop_hook_active": active,
    }


def test_repository_config_keeps_only_bounded_mailbox_stop_hook() -> None:
    config = tomllib.loads((ROOT / ".codex/config.toml").read_text())
    handlers = config["hooks"]["Stop"][0]["hooks"]

    assert [handler["type"] for handler in handlers] == ["command"]
    assert handlers[0]["command"].endswith('/scripts/codex-stop-mailbox"')
    assert handlers[0]["timeout"] == 5
    assert "codex-continuity-hook" not in json.dumps(config["hooks"])


def test_empty_mailbox_allows_stop_and_active_escape_skips_read() -> None:
    with MailboxServer([{
        "status": "ok", "delivery_ids": [], "has_more": False, "next_cursor": None,
    }]) as server:
        assert invoke(stop(), server.endpoint) == {}
        assert server.requests == [{"session_id": "session-1", "cursor": None}]
        assert invoke({"hook_event_name": "Stop", "stop_hook_active": True}, server.endpoint) == {}
        assert len(server.requests) == 1


def test_pending_mail_scans_every_page_and_requests_one_continuation() -> None:
    first, second = str(uuid4()), str(uuid4())
    with MailboxServer([
        {"status": "ok", "delivery_ids": [first], "has_more": True,
         "next_cursor": first},
        {"status": "ok", "delivery_ids": [second], "has_more": False,
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
    {"status": "ok", "delivery_ids": "wrong", "has_more": False,
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


def test_explicit_user_stop_skips_pending_mailbox(tmp_path: Path) -> None:
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "session-1",
              "turn_id": "turn", "prompt": "STOP"}
    subprocess.run(
        [HOOK, "--record-stop"],
        input=json.dumps(prompt), text=True, capture_output=True, check=True,
        env=os.environ | {"XDG_STATE_HOME": str(tmp_path)},
    )
    with MailboxServer([]) as server:
        assert invoke(stop() | {"turn_id": "turn"}, server.endpoint,
                      state_home=tmp_path) == {}
        assert server.requests == []


def configured_hooks(event: dict[str, Any], endpoint: str, state_home: Path) -> list[dict]:
    config = tomllib.loads((ROOT / ".codex/config.toml").read_text())
    return [json.loads(subprocess.run(
        ["sh", "-c", handler["command"]], cwd=ROOT,
        input=json.dumps(event), text=True, capture_output=True, check=True,
        env=os.environ | {"XDG_STATE_HOME": str(state_home),
                          "SWITCHSTAND_CODEX_STOP_ENDPOINT": endpoint},
    ).stdout) for group in config["hooks"].get(event["hook_event_name"], [])
        for handler in group["hooks"]]


def test_configured_plain_question_finishes_without_standing_assignment(tmp_path: Path) -> None:
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "session-1",
              "turn_id": "question", "prompt": "What is 2+2?"}
    with MailboxServer([{
        "status": "ok", "delivery_ids": [], "has_more": False, "next_cursor": None,
    }]) as server:
        outputs = configured_hooks(prompt, server.endpoint, tmp_path)
        assert any("FEEDBACK V1" in json.dumps(output) for output in outputs)
        assert "assignment is active" not in json.dumps(outputs)
        assert server.requests == []  # Prompt handlers perform no mailbox/network read.
        assert configured_hooks(stop() | {"turn_id": "question",
                                "last_assistant_message": "4."}, server.endpoint, tmp_path) == [{}]
    assert not (tmp_path / "switchstand/codex/notifications").exists()
    assert not (tmp_path / "switchstand/codex/continuity.jsonl").exists()


def test_configured_pending_mail_is_bounded_and_stop_does_not_leak(tmp_path: Path) -> None:
    delivery = str(uuid4())
    with MailboxServer([{
        "status": "ok", "delivery_ids": [delivery], "has_more": False, "next_cursor": None,
    }]) as server:
        prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "session-1",
                  "turn_id": "stopped", "prompt": "STOP"}
        configured_hooks(prompt, server.endpoint, tmp_path)
        assert configured_hooks(stop() | {"turn_id": "stopped"}, server.endpoint, tmp_path) == [{}]
        assert server.requests == []
        configured_hooks(prompt | {"turn_id": "next", "prompt": "What is 2+2?"},
                         server.endpoint, tmp_path)
        result, = configured_hooks(stop() | {"turn_id": "next"}, server.endpoint, tmp_path)
        assert result["decision"] == "block" and delivery in result["reason"]
        assert configured_hooks(stop(active=True), server.endpoint, tmp_path) == [{}]
        assert len(server.requests) == 1


@pytest.mark.parametrize("extra", [{"agent_id": "child"}, {"agent_type": "worker"},
                                   {"agent_transcript_path": "child.jsonl"}])
def test_child_prompt_cannot_overwrite_parent_stop(tmp_path: Path, extra: dict) -> None:
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "session-1",
              "turn_id": "turn", "prompt": "STOP"}
    with MailboxServer([]) as server:
        configured_hooks(prompt, server.endpoint, tmp_path)
        configured_hooks(prompt | {"prompt": "Continue"} | extra, server.endpoint, tmp_path)
        assert configured_hooks(stop() | {"turn_id": "turn"}, server.endpoint, tmp_path) == [{}]
        assert not server.requests


def test_malformed_prompt_mode_fails_open_without_mailbox_read(tmp_path: Path) -> None:
    with MailboxServer([]) as server:
        outputs = configured_hooks({"hook_event_name": "UserPromptSubmit", "prompt": "STOP"},
                                   server.endpoint, tmp_path)
        assert outputs[1]["continue"] is True
        assert "failed open" in outputs[1]["systemMessage"]
        assert not server.requests
