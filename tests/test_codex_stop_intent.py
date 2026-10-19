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
    return {"session_id": "session", "turn_id": "turn", "cwd": str(tmp_path),
            "transcript_path": native_source(
        tmp_path / "native.jsonl", "session", "turn", *messages,
    )}


@pytest.mark.parametrize("output_format", [
    "CHECKPOINT A and no hidden marker",
    "<!-- SWITCHSTAND_YIELD:HANDOFF --> and no other text",
    "<!-- SWITCHSTAND_YIELD:REAL_BLOCKER --> and no other text",
    "<!-- SWITCHSTAND_YIELD:ASSIGNMENT_COMPLETE --> and no other text",
])
def test_checkpoint_format_does_not_discard_current_action(
    tmp_path: Path, output_format: str,
) -> None:
    prompt = (
        "This is a benign continuation test, not a cancellation or pause of the assignment. "
        + ACTIVE + f" First end only this reply with exactly {output_format}. "
        "This first reply must precede the remaining work. On automatic continuation, perform "
        "the authorized receipt task and verify it. You may use the normal available file tools. "
        "No production work or external services are authorized."
    )
    assert HELPER["affirmed_action"](source(tmp_path, prompt), [ACTION]) == ACTION
    assert HELPER["affirmed_action"](source(tmp_path, ACTIVE), [ACTION]) == ACTION
    assert HELPER["affirmed_action"](source(tmp_path, ACTIVE), [ACTION + " then publish"]) is None


@pytest.mark.parametrize("suffix", [
    "STOP the assignment.", "Wait for my review.", "Do not use tools.",
])
def test_dedicated_output_format_never_hides_control(tmp_path: Path, suffix: str) -> None:
    prompt = ACTIVE + (
        " First end only this reply with exactly <!-- SWITCHSTAND_YIELD:HANDOFF --> "
        "and no other text. "
    ) + suffix
    assert HELPER["affirmed_action"](source(tmp_path, prompt), [ACTION]) is None


@pytest.mark.parametrize("output", [
    "<!-- SWITCHSTAND_YIELD:OTHER -->", "<!-- STOP -->", "HANDOFF when I return",
])
def test_unknown_output_controls_remain_unknown(tmp_path: Path, output: str) -> None:
    prompt = ACTIVE + f" First end only this reply with exactly {output} and no other text."
    assert HELPER["affirmed_action"](source(tmp_path, prompt), [ACTION]) is None


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


@pytest.mark.parametrize("damage", [
    "later_turn", "duplicate_start", "foreign_user", "missing_user_turn", "aborted",
])
def test_old_or_unattributable_user_turn_cannot_force_current_work(
    tmp_path: Path, damage: str,
) -> None:
    payload = source(tmp_path, ACTIVE)
    path = Path(payload["transcript_path"])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if damage in {"later_turn", "duplicate_start"}:
        rows.append({"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "later-stop" if damage == "later_turn" else "turn",
        }})
    elif damage == "aborted":
        rows.append({"type": "event_msg", "payload": {
            "type": "turn_aborted", "turn_id": "turn",
        }})
    else:
        extra = json.loads(json.dumps(rows[-1]))
        extra["payload"]["id"] = "foreign"
        metadata = extra["payload"]["internal_chat_message_metadata_passthrough"]
        if damage == "foreign_user":
            metadata["turn_id"] = "other"
        else:
            metadata.pop("turn_id")
        rows.append(extra)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        result = HELPER["affirmed_action"](payload, [ACTION])
    except ValueError as error:
        assert "UNKNOWN" in str(error)
    else:
        assert result is None


@pytest.mark.parametrize("instruction", [
    "Publish the release to production using the external service.",
    "Post the receipt to Slack.",
    "Send the email.",
])
def test_restricted_authority_cannot_be_overruled_by_an_exact_current_action(
    tmp_path: Path, instruction: str,
) -> None:
    action = "work: " + instruction
    text = ("This is an isolated continuation check, not a cancellation or pause of the assignment. "
            + "Your assignment remains active: " + instruction
            + " No production work or external services are authorized.")
    assert HELPER["affirmed_action"](source(tmp_path, text), [action]) is None


@pytest.mark.parametrize("damage", ["live_hook", "symlink", "parent", "cwd", "roots", "purpose"])
def test_no_production_restriction_requires_isolated_native_file_scope(
    tmp_path: Path, damage: str,
) -> None:
    destination = tmp_path / "receipt.output"
    if damage == "live_hook":
        destination = Path("/home/marco/switchstand/scripts/codex-continuity-hook")
    elif damage == "parent":
        destination = tmp_path / "../outside.output"
    elif damage == "symlink":
        destination.symlink_to("/home/marco/switchstand/scripts/codex-continuity-hook")
    instruction = f"Read {tmp_path}/receipt.input and write its exact contents to {destination}; read it back."
    prompt = ("This is an isolated continuation check, not a cancellation or pause of the assignment. "
              + "Your assignment remains active: " + instruction
              + " No production work or external services are authorized.")
    if damage == "purpose":
        prompt = prompt.split(". ", 1)[1]
    payload = source(tmp_path, prompt)
    if damage == "cwd":
        payload["cwd"] = "/home/marco/switchstand"
    elif damage == "roots":
        path = Path(payload["transcript_path"])
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]["payload"].pop("runtime_workspace_roots")
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert HELPER["affirmed_action"](payload, ["work: " + instruction]) is None


@pytest.mark.parametrize("metadata", [None, [], "source", 4])
def test_malformed_outer_metadata_is_caught_unknown(tmp_path: Path, metadata: object) -> None:
    payload = source(tmp_path, ACTIVE)
    path = Path(payload["transcript_path"])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[-1]["metadata"] = metadata
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(TypeError, match="UNKNOWN"):
        HELPER["affirmed_action"](payload, [ACTION])


def interrupted_source(tmp_path: Path, *messages: str) -> dict[str, str]:
    payload = source(tmp_path, *messages)
    path = Path(payload["transcript_path"])
    previous = [json.loads(line) for line in path.read_text().splitlines()]
    current = source(tmp_path, ".")
    following = [json.loads(line) for line in path.read_text().splitlines()][1:]
    for record in following:
        item = record["payload"]
        if item.get("type") == "task_started":
            item["turn_id"] = "dot"
            item["started_at"] = 101
        elif item.get("role") == "user":
            item["internal_chat_message_metadata_passthrough"]["turn_id"] = "dot"
    previous.append({"type": "event_msg", "payload": {
        "type": "turn_aborted", "turn_id": "turn", "reason": "interrupted", "completed_at": 100,
    }})
    path.write_text("".join(json.dumps(row) + "\n" for row in previous + following))
    return current | {"turn_id": "dot"}


def test_announced_end_turn_and_immediate_interrupted_dot_keep_assignment(tmp_path: Path) -> None:
    announcement = "testing if new continuatio works."
    assert HELPER["affirmed_action"](source(tmp_path, announcement, "end your turn"), [ACTION]) \
        == ACTION
    assert HELPER["affirmed_action"](
        interrupted_source(tmp_path, announcement, "end your turn"), [ACTION],
    ) == ACTION


@pytest.mark.parametrize("damage", [
    "stop", "pause", "question", "late", "negative_time", "missing_time", "correction",
    "unknown_user", "call", "completed", "wrong_abort", "unannounced", "intervening_turn",
])
def test_interrupted_context_never_overrides_controls_or_ambiguous_effects(
    tmp_path: Path, damage: str,
) -> None:
    messages = ["checking whether the continuation is working.", "end this reply"]
    if damage in {"stop", "pause", "question"}:
        messages.append({"stop": "STOP", "pause": "pause the task",
                         "question": "What happens next?"}[damage])
    elif damage == "unannounced":
        messages = ["end your turn"]
    payload = interrupted_source(tmp_path, *messages)
    path = Path(payload["transcript_path"])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    abort = next(row["payload"] for row in rows if row["payload"].get("type") == "turn_aborted")
    if damage == "late":
        abort["completed_at"] = 1
    elif damage == "negative_time":
        abort["completed_at"] = 102
    elif damage == "missing_time":
        abort.pop("completed_at")
    elif damage == "wrong_abort":
        abort["turn_id"] = "other"
    elif damage in {"correction", "call", "completed", "unknown_user", "intervening_turn"}:
        extra = {
            "correction": {"type": "event_msg", "payload": {
                "type": "item_completed", "turn_id": "turn", "item": {"type": "HookPrompt"},
            }},
            "call": {"type": "response_item", "payload": {"type": "custom_tool_call"}},
            "completed": {"type": "event_msg", "payload": {"type": "task_complete"}},
            "unknown_user": {"type": "response_item", "payload": {
                "role": "user", "internal_chat_message_metadata_passthrough": {
                    "turn_id": "turn", "content_item_kinds": ["unknown"],
                },
            }},
            "intervening_turn": {"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "other",
            }},
        }[damage]
        rows.insert(-2, extra)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        result = HELPER["affirmed_action"](payload, [ACTION])
    except ValueError as error:
        assert "UNKNOWN" in str(error)
    else:
        assert result is None


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
