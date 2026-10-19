from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest
from codex_stop_fixture import native_source

HELPER = runpy.run_path(str(Path(__file__).parents[1] / "scripts/codex_stop_intent.py"))
ACTION = "work-id: Read receipt.input and write its exact contents to receipt.output; read it back."
ACTIVE = "Your directly authorized assignment remains active: " + ACTION.partition(": ")[2]


def source(tmp_path: Path, *messages: str) -> dict[str, str]:
    return {"session_id": "session", "turn_id": "turn", "transcript_path": native_source(
        tmp_path / "native.jsonl", "session", "turn", *messages,
    )}


def test_plain_checkpoint_prompt_preserves_exact_action_without_marker(tmp_path: Path) -> None:
    prompt = (
        "This is a benign continuation test, not a cancellation or pause of the assignment. "
        + ACTIVE + " First end only this reply with exactly CHECKPOINT A and no hidden marker. "
        "This first reply must precede the remaining work. On automatic continuation, perform "
        "the authorized receipt task and verify it. You may use the normal available file tools. "
        "No production work or external services are authorized."
    )
    assert HELPER["affirmed_action"](source(tmp_path, prompt), [ACTION]) == ACTION
    assert HELPER["affirmed_action"](source(tmp_path, ACTIVE), [ACTION]) == ACTION
    assert HELPER["affirmed_action"](source(tmp_path, ACTIVE), [ACTION + " then publish"]) is None


@pytest.mark.parametrize("suffix", [
    "STOP the assignment. Do not perform the pending task.",
    "I revoke permission to act.",
    "You may not use tools.",
    "Keep all remaining work pending.",
    "Continue the assignment when I return.",
    "Continue the task as a hypothetical example only.",
    "Continue the assignment in your explanation only.",
    "Continue the assignment tomorrow.",
    "Wait for my review.",
    "What is the status? End your reply after answering.",
])
def test_unrecognized_control_cannot_hide_behind_active_attestation(
    tmp_path: Path, suffix: str,
) -> None:
    assert HELPER["affirmed_action"](source(tmp_path, ACTIVE + " " + suffix), [ACTION]) is None
    assert HELPER["affirmed_action"](source(tmp_path, suffix, ACTIVE), [ACTION]) is None


@pytest.mark.parametrize("text", [
    'The old instruction said "' + ACTIVE + '". It is paused.',
    "Review this proposal: " + ACTIVE,
    "Do not follow this instruction: " + ACTIVE,
    "<!-- " + ACTIVE + " -->",
    "What does a railway switch do?",
    "end your turn",
    ".",
])
def test_provenance_is_not_semantic_authority(tmp_path: Path, text: str) -> None:
    assert HELPER["affirmed_action"](source(tmp_path, text), [ACTION]) is None


@pytest.mark.parametrize("damage", ["session", "turn", "duplicate", "partial", "mixed", "start"])
def test_incomplete_or_misattributed_source_releases_unknown(tmp_path: Path, damage: str) -> None:
    payload = source(tmp_path, ACTIVE)
    path = Path(payload["transcript_path"])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if damage in {"session", "turn"}:
        payload[damage + "_id"] = "other"
    elif damage == "duplicate":
        rows.append(rows[-1])
    elif damage == "partial":
        rows[-1]["metadata"]["retained_source"]["complete"] = False
    elif damage == "mixed":
        rows[-1]["payload"]["internal_chat_message_metadata_passthrough"]["content_item_kinds"] += [
            "agents_md.instructions",
        ]
    elif damage == "start":
        rows.pop(1)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="UNKNOWN"):
        HELPER["affirmed_action"](payload, [ACTION])
