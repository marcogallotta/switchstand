from __future__ import annotations

import io
import json
import runpy
import subprocess
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[1]
HOOK = ROOT / "scripts/codex-feedback-trigger-hook"


def invoke(payload: object, *, raw: str | None = None) -> dict[str, object]:
    result = subprocess.run(
        [HOOK],
        input=raw if raw is not None else json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("payload", [
    {"hook_event_name": "UserPromptSubmit", "prompt": "This is still broken."},
    {"hook_event_name": "SessionStart", "source": "compact"},
])
def test_each_turn_and_compaction_restore_the_feedback_obligation(
    payload: dict[str, str],
) -> None:
    output = invoke(payload)

    context = output["hookSpecificOutput"]["additionalContext"]
    assert "Respond to Marco immediately" in context
    assert "Once observed, finish one existing Tracker report or attributable reuse" in context
    assert "before unrelated work" in context
    assert "preserve this obligation across interruption" in context
    assert "Frustration or a routine Human Review decision alone is not a report trigger" in context
    assert "Respect STOP and explicit no-logging" in context
    assert "Route process/review burden via file_process" in context
    assert "F:– is false while capture is owed" in context
    assert "F:logged needs confirmed readback" in context
    assert "Inspect ALL_TOOLS before F:unavailable" in context
    assert "No parallel path" in context
    assert len(context) < (2600 if payload["hook_event_name"] == "SessionStart" else 1200)


def test_only_compaction_prepends_recovery_to_the_unchanged_turn_gate() -> None:
    prompt = invoke({"hook_event_name": "UserPromptSubmit", "prompt": "Continue."})
    compact = invoke({"hook_event_name": "SessionStart", "source": "compact"})

    prompt_hook = prompt["hookSpecificOutput"]
    compact_hook = compact["hookSpecificOutput"]
    assert prompt_hook["hookEventName"] == "UserPromptSubmit"
    assert compact_hook["hookEventName"] == "SessionStart"
    gate = prompt_hook["additionalContext"]
    recovery, restored_gate = compact_hook["additionalContext"].split("\n\n", 1)
    assert gate.startswith("FEEDBACK V1.")
    assert restored_gate == gate
    assert recovery.startswith("POST-COMPACT RECOVERY.")
    assert "ORIGINAL APPROVED" in recovery
    assert "work_id=<exact assigned WorkId>" in recovery
    assert "docs/coordinator-tracker-contract.md" in recovery
    assert "STOP" in recovery


@pytest.mark.parametrize("payload, raw", [
    ([], None),
    ({"hook_event_name": "UserPromptSubmit"}, None),
    ({"hook_event_name": "SessionStart", "source": "resume"}, None),
    ({}, "not-json"),
])
def test_malformed_or_misrouted_event_fails_open_visibly(
    payload: object, raw: str | None,
) -> None:
    output = invoke(payload, raw=raw)

    assert output["continue"] is True
    assert "failed open" in output["systemMessage"]


def test_repository_config_injects_without_network_or_interrupt_hooks() -> None:
    config = tomllib.loads((ROOT / ".codex/config.toml").read_text())
    hooks = config["hooks"]

    prompt = hooks["UserPromptSubmit"][0]
    compact = hooks["SessionStart"][0]
    post_tool = hooks["PostToolUse"][0]
    assert "matcher" not in prompt
    assert "matcher" not in post_tool
    assert compact["matcher"] == "^compact$"
    for handler in (prompt["hooks"][0], compact["hooks"][0], post_tool["hooks"][0]):
        assert handler["type"] == "command"
        assert handler["command"].endswith('/scripts/codex-feedback-trigger-hook"')
        assert handler["timeout"] == 1
        assert handler["additionalContextLimit"] == 700
    assert "Interrupt" not in hooks


@pytest.fixture
def notification_clock(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    main = runpy.run_path(str(HOOK))["main"]

    def call(now, actor=None):
        event = {"hook_event_name": "PostToolUse", "session_id": "session"}
        if actor is not None:
            event["agent_id"] = actor
        with patch("sys.stdin", io.StringIO(json.dumps(event))), patch("time.time", return_value=now):
            assert main() == 0
        output = json.loads(capsys.readouterr().out)
        return output.get("hookSpecificOutput", {}).get("additionalContext", "")

    return call


def test_notifications_keep_due_time_and_repeat_light_light_light_hard(notification_clock):
    call = notification_clock
    assert call(100) == ""
    assert call(399) == ""  # Unrelated tool activity must not restart the clock.
    for now, level in [(400, "LIGHT"), (700, "LIGHT"), (1000, "LIGHT"), (1300, "HARD"), (1600, "LIGHT")]:
        context = call(now)
        assert f"{level} check" in context
        assert "new messages and actionable feedback" in context
        assert "ORIGINAL APPROVED objective" in context
        assert "not mean the checks are complete" in context
        assert call(now) == ""
    assert "LIGHT check" in call(9999)  # A missed interval delivers once, without a burst.
    assert call(9999) == ""
    assert call(10298) == ""


def test_child_activity_cannot_suppress_parent_or_other_child(notification_clock, tmp_path):
    call = notification_clock
    assert call(0) == call(20, "child-a") == call(30, "child-b") == ""
    assert call(299, "child-a") == ""
    assert "LIGHT check" in call(300)
    assert call(300, "child-a") == ""
    assert "LIGHT check" in call(320, "child-a")
    assert "LIGHT check" in call(330, "child-b")
    records = list(tmp_path.rglob("*.json"))
    assert len(records) == 3
    assert all(set(json.loads(path.read_text())) == {"due", "counter"} for path in records)


def test_concurrent_due_callbacks_deliver_once(notification_clock):
    assert notification_clock(0) == ""
    payload = {"hook_event_name": "PostToolUse", "session_id": "session"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(invoke, [payload] * 4))
    contexts = [out.get("hookSpecificOutput", {}).get("additionalContext", "") for out in outputs]
    assert sum("LIGHT check" in context for context in contexts) == 1


def test_missing_identity_and_corrupt_timer_fail_open(notification_clock, tmp_path):
    assert invoke({"hook_event_name": "PostToolUse"})["continue"] is True
    assert notification_clock(0) == ""
    next(tmp_path.rglob("*.json")).write_text("{}")
    output = invoke({"hook_event_name": "PostToolUse", "session_id": "session"})
    assert output["continue"] is True and "failed open" in output["systemMessage"]
