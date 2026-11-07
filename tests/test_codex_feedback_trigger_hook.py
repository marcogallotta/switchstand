from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

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
    assert "Once observed, file it with the Feedback file_* tools" in context
    assert "before unrelated work" in context
    assert "preserve this obligation across interruption" in context
    assert "Inspect ALL_TOOLS before saying Feedback is unavailable" in context
    assert "F:" not in context
    assert "File every Human Review outcome, good or bad, with file_review_feedback" in context
    assert "Respect STOP and explicit no-logging" in context
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
    assert "If you have an assigned WorkId" in recovery
    assert "a question does not create work or a Coordinator role" in recovery
    assert "Worker and Coordinator parents use context-free native forks" in recovery
    assert "delegated native Workers execute bounded assignments directly" in recovery
    assert "Ordinary sessions work directly" not in recovery
    assert "docs/coordinator-tracker-contract.md" not in recovery
    assert "docs/recovery-plan.md" not in recovery
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
    assert "matcher" not in prompt
    assert "PostToolUse" not in hooks
    assert compact["matcher"] == "^compact$"
    for handler in (prompt["hooks"][0], compact["hooks"][0]):
        assert handler["type"] == "command"
        assert handler["command"].endswith('/scripts/codex-feedback-trigger-hook"')
        assert handler["timeout"] == 1
        assert handler["additionalContextLimit"] == 700
    assert "Interrupt" not in hooks


@pytest.mark.parametrize("actor", [None, "child", ""])
def test_old_post_tool_registration_is_inert_without_state(tmp_path, monkeypatch, actor):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    payload = {"hook_event_name": "PostToolUse", "session_id": "session"}
    if actor is not None:
        payload["agent_id"] = actor
    assert invoke(payload) == {}
    assert invoke({"hook_event_name": "PostToolUse"}) == {}
    assert not list(tmp_path.iterdir())
