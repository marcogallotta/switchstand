from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from codex_stop_fixture import native_source

ROOT = Path(__file__).parents[1]
HOOK = ROOT / "scripts/codex-continuity-hook"


@pytest.fixture(autouse=True)
def opted_in_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".config/switchstand/codex-stop-head.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"session_id": "session"}))


def invoke(
    tmp_path: Path,
    payload: object,
    *,
    lifetime: str = "ASSIGNMENT",
    raw: str | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    if isinstance(payload, dict) and payload.get("hook_event_name") == "Stop":
        payload = payload | {"transcript_path": native_source(
            tmp_path / "native.jsonl", payload.get("session_id", ""),
            payload.get("turn_id", ""),
            "Your assignment remains active: poll the pending independent review",
        )}
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
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        monkeypatch.setenv(
            "SWITCHSTAND_CODEX_STOP_ENDPOINT", f"http://127.0.0.1:{server.server_port}",
        )
        output, events = invoke(tmp_path, stop(message, active=active))
        server.shutdown()
        thread.join()
    assert output.get("decision") != "block"
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
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        monkeypatch.setenv(
            "SWITCHSTAND_CODEX_STOP_ENDPOINT", f"http://127.0.0.1:{server.server_port}",
        )
        message = {
            "checkpoint": "Checkpoint only",
            "prose_handoff": "I am handing off. <!-- SWITCHSTAND_YIELD:HANDOFF -->",
        }.get(marker, f"<!-- SWITCHSTAND_YIELD:{marker} -->")
        first, _ = invoke(tmp_path, stop(message))
        monkeypatch.setenv("SWITCHSTAND_CODEX_STOP_ENDPOINT", "http://127.0.0.1:1")
        second, events = invoke(tmp_path, stop(message, active=True))
        if marker == "HANDOFF" and evidence == "runnable":
            monkeypatch.setenv(
                "SWITCHSTAND_CODEX_STOP_ENDPOINT", f"http://127.0.0.1:{server.server_port}",
            )
            new_turn, _ = invoke(tmp_path, stop(message) | {"turn_id": "new-turn"})
            assert new_turn["decision"] == "block"
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
    expected_reads = 2 if marker == "HANDOFF" and evidence == "runnable" else 1
    assert requests == [{"session_id": "session", "check_obligations": True}] * expected_reads


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


@pytest.mark.parametrize("message,lifetime,blocked", [
    ("Checkpoint only", "ASSIGNMENT", True),
    ("Quoted `<!-- SWITCHSTAND_YIELD:HANDOFF -->` marker.", "ASSIGNMENT", True),
    ("<!-- SWITCHSTAND_YIELD:HANDOFF -->", "STANDING", False),
    ("<!-- SWITCHSTAND_YIELD:ASSIGNMENT_COMPLETE -->", "STANDING", True),
    ("<!-- SWITCHSTAND_YIELD:ASSIGNMENT_COMPLETE -->", "ASSIGNMENT", False),
])
def test_off_target_legacy_continuity_survives_tracker_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str, lifetime: str, blocked: bool,
) -> None:
    (tmp_path / ".config/switchstand/codex-stop-head.json").unlink()
    monkeypatch.setenv("SWITCHSTAND_CODEX_STOP_ENDPOINT", "http://127.0.0.1:1")
    output, events = invoke(tmp_path, stop(message), lifetime=lifetime)
    assert (output.get("decision") == "block") is blocked
    assert bool(events[-1]["blocked"]) is blocked
    if "ASSIGNMENT_COMPLETE" in message:
        assert events[-1]["yield_marker"] == "ASSIGNMENT_COMPLETE"


def test_other_session_does_not_inherit_local_target_unknown_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWITCHSTAND_CODEX_STOP_ENDPOINT", "http://127.0.0.1:1")
    output, _ = invoke(tmp_path, stop("Checkpoint only") | {"session_id": "other-session"})
    assert output["decision"] == "block"


def test_canonical_binding_alone_never_opts_session_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    (tmp_path / ".config/switchstand/codex-stop-head.json").unlink()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok","coordinator":true,"currentness":"CURRENT",'
                             b'"actions":["canonical work: continue"]}')

        def log_message(self, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv(
            "SWITCHSTAND_CODEX_STOP_ENDPOINT", f"http://127.0.0.1:{server.server_port}",
        )
        first, _ = invoke(tmp_path, stop("Hello"))
        monkeypatch.setenv("SWITCHSTAND_CODEX_STOP_ENDPOINT", "http://127.0.0.1:1")
        second, events = invoke(tmp_path, stop("Hello", active=True))
        server.shutdown()
        thread.join()
    assert first["decision"] == second["decision"] == "block"
    assert events[-1]["blocked"] == 1
    assert not requests


@pytest.mark.parametrize("active", [False, True])
def test_off_target_subagent_keeps_legacy_continuity(tmp_path: Path, active: bool) -> None:
    output, _ = invoke(tmp_path, stop("Checkpoint only", active=active) | {"agent_id": "child"})
    assert output["decision"] == "block"
