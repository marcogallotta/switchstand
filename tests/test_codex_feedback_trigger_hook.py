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
    assert "bad/frustrating or contains a Human Review outcome" in context
    assert "mcp__switchstand__file_*" in context
    assert "complete result" in context and "confirmed readback" in context
    assert "Preserve that obligation across steering or an interrupted turn" in context
    assert "F:– is false" in context
    assert "F:unavailable is false unless ALL_TOOLS was inspected" in context
    assert "Do not create another reporting path" in context
    assert len(context) < 1200


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
    assert compact["matcher"] == "^compact$"
    for handler in (prompt["hooks"][0], compact["hooks"][0]):
        assert handler["type"] == "command"
        assert handler["command"].endswith('/scripts/codex-feedback-trigger-hook"')
        assert handler["timeout"] == 1
        assert handler["additionalContextLimit"] == 700
    assert "Interrupt" not in hooks
