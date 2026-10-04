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
def test_missing_marker_blocks_even_after_prior_stop_continuation(
    tmp_path: Path, active: bool,
) -> None:
    output, events = invoke(tmp_path, stop("Checkpoint only", active=active))

    assert output["decision"] == "block"
    assert "still active" in output["reason"]
    assert events[0]["blocked"] == 1
    assert events[0]["yield_marker"] is None


def test_stop_continuation_names_every_marker_accepted_for_the_lifetime(
    tmp_path: Path,
) -> None:
    assignment, _ = invoke(tmp_path, stop("Checkpoint only"))
    standing, _ = invoke(tmp_path, stop("Checkpoint only"), lifetime="STANDING")

    for marker in ("ASSIGNMENT_COMPLETE", "REAL_BLOCKER", "HANDOFF", "USER_STOP"):
        syntax = f"`<!-- SWITCHSTAND_YIELD:{marker} -->`"
        assert syntax in assignment["reason"]
        assert (syntax in standing["reason"]) == (marker != "ASSIGNMENT_COMPLETE")


@pytest.mark.parametrize("marker", ["REAL_BLOCKER", "HANDOFF", "USER_STOP"])
def test_common_deliberate_yields_are_allowed(tmp_path: Path, marker: str) -> None:
    output, events = invoke(
        tmp_path, stop(f"Visible reason.\n<!-- SWITCHSTAND_YIELD:{marker} -->")
    )

    assert output == {}
    assert events[0]["blocked"] == 0
    assert events[0]["yield_marker"] == marker


def test_assignment_complete_depends_on_run_lifetime(tmp_path: Path) -> None:
    message = "Done.\n<!-- SWITCHSTAND_YIELD:ASSIGNMENT_COMPLETE -->"

    assignment, _ = invoke(tmp_path, stop(message))
    standing, events = invoke(tmp_path, stop(message), lifetime="STANDING")

    assert assignment == {}
    assert standing["decision"] == "block"
    assert events[-1]["blocked"] == 1
    assert events[-1]["yield_marker"] == "ASSIGNMENT_COMPLETE"


@pytest.mark.parametrize("message", [
    "Quoted `<!-- SWITCHSTAND_YIELD:HANDOFF -->` marker.",
    "<!-- SWITCHSTAND_YIELD:WAITING -->",
    "<!-- SWITCHSTAND_YIELD:HANDOFF -->\nmore text",
    "Example <!-- SWITCHSTAND_YIELD:HANDOFF -->\n<!-- SWITCHSTAND_YIELD:HANDOFF -->",
])
def test_only_one_exact_terminal_marker_is_accepted(tmp_path: Path, message: str) -> None:
    output, events = invoke(tmp_path, stop(message))

    assert output["decision"] == "block"
    assert events[0]["blocked"] == 1


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
