from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
HOOK = ROOT / "scripts/codex-continuity-hook"


def invoke(
    tmp_path: Path,
    payload: object,
    *,
    lifetime: str = "ASSIGNMENT",
    raw: str | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    telemetry = tmp_path / "private/telemetry.jsonl"
    result = subprocess.run(
        [HOOK, "--mode", "PILOT", "--lifetime", lifetime, "--telemetry", telemetry],
        input=raw if raw is not None else json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    events = [json.loads(line) for line in telemetry.read_text().splitlines()] \
        if telemetry.exists() else []
    return json.loads(result.stdout), events


def stop(message: object, *, active: bool = False) -> dict[str, object]:
    return {
        "hook_event_name": "Stop",
        "session_id": "session",
        "turn_id": "turn",
        "stop_hook_active": active,
        "last_assistant_message": message,
    }


def test_root_prompt_adds_resume_context_but_subagent_payload_does_not(tmp_path: Path) -> None:
    root, events = invoke(tmp_path, {
        "hook_event_name": "UserPromptSubmit", "prompt": "status?", "session_id": "root",
    })
    child, child_events = invoke(tmp_path, {
        "hook_event_name": "UserPromptSubmit", "prompt": "work", "session_id": "root",
        "agent_id": "child",
    })

    output = root["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    assert "resume" in output["additionalContext"]
    assert child == {}
    assert events == child_events == []


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("message", [
    "Checkpoint only", "Hello", "Stopped.", "<!-- SWITCHSTAND_YIELD:USER_STOP -->",
    "Quoted `<!-- SWITCHSTAND_YIELD:HANDOFF -->` marker.",
])
def test_conversation_without_runnable_work_and_explicit_stop_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active: bool, message: str,
) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok","currentness":"CURRENT","actions":[]}')

        def log_message(self, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv(
            "SWITCHSTAND_CODEX_STOP_ENDPOINT", f"http://127.0.0.1:{server.server_port}",
        )
        output, events = invoke(tmp_path, stop(message, active=active))
        server.shutdown()
        thread.join()
    assert output == {}
    assert events[0]["blocked"] == 0


@pytest.mark.parametrize("evidence", ["runnable", "complete", "stale", "missing", "contradictory"])
@pytest.mark.parametrize("marker", [
    "HANDOFF", "REAL_BLOCKER", "ASSIGNMENT_COMPLETE", "checkpoint", "prose_handoff",
])
def test_false_yield_gets_one_correction_then_releases_without_losing_obligation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str, evidence: str,
) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    requests = []
    action = "work-123: poll the pending independent review"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()
            body = {"status": "ok", "currentness": "CURRENT", "actions": [action]}
            if evidence == "complete":
                body["actions"] = []
            elif evidence == "stale":
                body["currentness"] = "STALE"
            elif evidence in {"missing", "contradictory"}:
                body = {"status": "unknown", "reason": evidence}
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv(
            "SWITCHSTAND_CODEX_STOP_ENDPOINT", f"http://127.0.0.1:{server.server_port}",
        )
        message = {
            "checkpoint": "Checkpoint only",
            "prose_handoff": "I am handing off. <!-- SWITCHSTAND_YIELD:HANDOFF -->",
        }.get(marker, f"<!-- SWITCHSTAND_YIELD:{marker} -->")
        first, _ = invoke(tmp_path, stop(message))
        second, events = invoke(tmp_path, stop(message, active=True))
        server.shutdown()
        thread.join()
    if evidence == "runnable":
        assert first["decision"] == "block"
        assert action in first["reason"]
        assert "preserve" in first["reason"]
    elif evidence == "complete":
        assert first == {}
    else:
        assert first["continue"] is True
        assert "UNKNOWN" in first["systemMessage"]
    assert second == {}
    assert events[-1]["blocked"] == 0
    if marker == "ASSIGNMENT_COMPLETE":
        assert events[-1]["attempted_yield_marker"] == marker
        assert all(event.get("yield_marker") != marker for event in events)
    assert requests == [{"session_id": "session", "check_obligations": True}]


def test_unavailable_tracker_releases_without_completion_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWITCHSTAND_CODEX_STOP_ENDPOINT", "http://127.0.0.1:1")
    output, events = invoke(tmp_path, stop("<!-- SWITCHSTAND_YIELD:HANDOFF -->"))
    assert output["continue"] is True
    assert "failed open" in output["systemMessage"]
    assert events[-1]["event"] == "hook_error"


@pytest.mark.parametrize("payload, raw", [
    (stop(42), None),
    ({"hook_event_name": "Unknown"}, None),
    ({}, "not-json"),
])
def test_malformed_payload_fails_open_visibly(
    tmp_path: Path, payload: object, raw: str | None,
) -> None:
    output, events = invoke(tmp_path, payload, raw=raw)

    assert output["continue"] is True
    assert "failed open" in output["systemMessage"]
    assert events[0]["event"] == "hook_error"
    assert events[0]["hook_error"]


def test_child_yield_never_queries_coordinator(tmp_path: Path) -> None:
    output, _ = invoke(tmp_path, stop("<!-- SWITCHSTAND_YIELD:HANDOFF -->") | {"agent_id": "child"})
    assert output == {}


def test_explicit_stop_skips_even_a_runnable_tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWITCHSTAND_CODEX_STOP_ENDPOINT", "http://127.0.0.1:1")
    output, events = invoke(tmp_path, stop("<!-- SWITCHSTAND_YIELD:USER_STOP -->"))
    assert output == {}
    assert events[0]["hook_error"] is None
