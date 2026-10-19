"""Conservative positive work-intent evidence for the two native Stop handlers.

This recognizes an explicit reaffirmation and work instruction, not arbitrary
natural language. Unrecognized input is UNKNOWN, never cancellation/completion.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

TAIL_BYTES = 1024 * 1024
ACTIVE_ASSIGNMENT = re.compile(
    r"(?:^|(?<=[.!])\s+)your (?:directly )?(?:authorized )?assignment "
    r"(?:remains|is still) active\s*:\s*", re.IGNORECASE,
)
# Whole ancillary clauses only. No arbitrary suffix, quotation, condition or
# countermand is ignored. These clauses never supply action authority.
AUXILIARY = re.compile(
    r"(?:this is a (?:(?:benign|isolated|ordinary|native|controlled) )?"
    r"continuation (?:test|check|trial), not a cancellation or pause of the assignment|"
    r"first end only (?:this|your) (?:reply|turn) with exactly [a-z0-9_ -]+ "
    r"and no hidden marker|"
    r"first end only (?:this|your) (?:reply|turn) with exactly "
    r"<!-- SWITCHSTAND_YIELD:(?:HANDOFF|REAL_BLOCKER|ASSIGNMENT_COMPLETE) --> "
    r"and no other text|"
    r"this first (?:reply|turn) must precede the (?:remaining|pending) work|"
    r"on automatic continuation, (?:perform|continue|resume) the authorized "
    r"[a-z]+ (?:task|work|assignment) and verify it|"
    r"you may use the normal available file tools|"
    r"no production work or external services are authorized)\.?", re.IGNORECASE,
)
LOCAL_FILE_ACTION = re.compile(
    r"Read (?P<input>/[^\s;]+|[\w.-]+) and write its exact contents to "
    r"(?P<output>/[^\s;]+|[\w.-]+); read it back\."
)
CONTINUATION_CHECK = re.compile(
    r"(?:testing|checking|trying) (?:if|whether) (?:(?:the|this|new) )?"
    r"(?:continuatio[n]?|resumption) (?:works|is working)\.?\s+"
    r"(?:please )?end (?:your|this) (?:turn|reply)[.!]?", re.IGNORECASE,
)


def _native_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Read a bounded native source window; never infer origin from assistant text."""
    session_id, turn_id = payload.get("session_id"), payload.get("turn_id")
    path = payload.get("transcript_path")
    if not all(isinstance(value, str) and value for value in (session_id, turn_id, path)):
        raise ValueError("UNKNOWN originating user turn: missing native source identity")
    with Path(path).open("rb") as handle:
        header = json.loads(handle.readline(TAIL_BYTES))
        if (
            not isinstance(header, dict) or header.get("type") != "session_meta"
            or not isinstance(header.get("payload"), dict)
            or header["payload"].get("id") != session_id
        ):
            raise ValueError("UNKNOWN originating user turn: session mismatch")
        handle.seek(0, 2)
        offset = max(0, handle.tell() - TAIL_BYTES)
        handle.seek(offset)
        if offset:
            handle.readline()  # Discard a potentially partial first record.
        lines = handle.read(TAIL_BYTES).splitlines()
    records = []
    for line in lines:
        record = json.loads(line)
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict) \
                or not isinstance(record.get("type"), str):
            raise TypeError("UNKNOWN originating user turn: malformed source record")
        records.append(record)
    if not records or records[0]["type"] != "session_meta":
        records.insert(0, header)
    return records


def _user_text(records: list[dict[str, Any]], turn_id: str) -> str:
    matches: list[str] = []
    message_ids: set[str] = set()
    started = False
    for record in records:
        item = record["payload"]
        if (
            record.get("type") == "event_msg" and item.get("type") == "task_started"
            and item.get("turn_id") == turn_id
        ):
            if started:
                raise ValueError("UNKNOWN originating user turn: duplicate start")
            started = True
        if record.get("type") != "response_item" or item.get("role") != "user":
            continue
        metadata = item.get("internal_chat_message_metadata_passthrough", {})
        if not isinstance(metadata, dict):
            raise TypeError("UNKNOWN originating user turn: malformed metadata")
        if metadata.get("turn_id") != turn_id:
            if started:
                raise ValueError("UNKNOWN originating user turn: foreign user input")
            continue
        kinds = metadata.get("content_item_kinds")
        if kinds == ["user.text"]:
            source_metadata = record.get("metadata")
            if not isinstance(source_metadata, dict):
                raise TypeError("UNKNOWN originating user turn: malformed retained metadata")
            retained = source_metadata.get("retained_source", {})
            if not started or not isinstance(retained, dict) or retained.get("complete") is not True:
                raise ValueError("UNKNOWN originating user turn: incomplete source window")
            message_id = item.get("id")
            if not isinstance(message_id, str) or message_id in message_ids:
                raise ValueError("UNKNOWN originating user turn: absent or duplicate message id")
            message_ids.add(message_id)
            content = item.get("content")
            if not isinstance(content, list) or len(content) != 1:
                raise ValueError("UNKNOWN originating user turn: mixed content")
            if (
                not isinstance(content[0], dict) or content[0].get("type") != "input_text"
                or not isinstance(content[0].get("text"), str)
            ):
                raise ValueError("UNKNOWN originating user turn: nontext content")
            matches.append(content[0]["text"])
        elif isinstance(kinds, list) and "user.text" in kinds:
            raise ValueError("UNKNOWN originating user turn: mixed source kinds")
        elif not isinstance(kinds, list) or not kinds or set(kinds) - {
            "agents_md.instructions", "environments.environment_context",
        }:
            raise ValueError("UNKNOWN originating user turn: unsupported user source")
    if not matches:
        raise ValueError("UNKNOWN originating user turn: absent user.text")
    return "\n".join(matches)


def _interrupted_check(records: list[dict[str, Any]], turn_id: str) -> bool:
    starts = [index for index, record in enumerate(records) if record["type"] == "event_msg"
              and record["payload"].get("type") == "task_started"]
    if len(starts) < 2 or records[starts[-1]]["payload"].get("turn_id") != turn_id:
        return False
    previous_start, current_start = starts[-2:]
    previous = records[previous_start:current_start]
    previous_id = previous[0]["payload"].get("turn_id")
    if not isinstance(previous_id, str) or previous_id == turn_id:
        return False
    if sum(records[index]["payload"].get("turn_id") == previous_id for index in starts) != 1:
        return False
    aborts = [index for index, record in enumerate(previous) if record["type"] == "event_msg"
              and record["payload"].get("type") == "turn_aborted"]
    if len(aborts) != 1:
        return False
    abort = previous[aborts[0]]["payload"]
    if abort.get("turn_id") != previous_id or abort.get("reason") != "interrupted":
        return False
    ended, started = abort.get("completed_at"), records[current_start]["payload"].get("started_at")
    if type(ended) is not int or type(started) is not int or not 0 <= started - ended <= 10:
        return False
    for index, record in enumerate(previous):
        item = record["payload"]
        # Native HookPrompt is the source-backed receipt of a prior correction.
        # Calls may have partly executed before interruption; never blindly retry.
        event_item = item.get("item", {})
        if not isinstance(event_item, dict):
            return False
        if item.get("type") == "task_complete" or event_item.get("type") == "HookPrompt" \
                or "_call" in str(item.get("type")):
            return False
        if index > aborts[0] and record["type"] == "response_item" and item.get("role") == "user":
            return False
    return bool(CONTINUATION_CHECK.fullmatch(_user_text(previous, previous_id)))


def _isolated_file_action(
    payload: dict[str, Any], records: list[dict[str, Any]], instruction: str, clauses: list[str],
) -> bool:
    match = LOCAL_FILE_ACTION.fullmatch(instruction)
    if not match or not any(clause.lower().startswith("this is ") and AUXILIARY.fullmatch(clause)
                            for clause in clauses):
        return False
    source = records[0]["payload"]
    cwd = source.get("cwd")
    if not isinstance(cwd, str) or payload.get("cwd") != cwd \
            or source.get("runtime_workspace_roots") != [cwd] or not Path(cwd).is_absolute():
        return False
    try:
        root = Path(cwd).resolve()
        if root == Path("/tmp") or not root.is_relative_to("/tmp"):
            return False
        return all((root / match[name]).resolve().is_relative_to(root)
                   for name in ("input", "output"))
    except (OSError, RuntimeError):
        return False


def affirmed_action(payload: dict[str, Any], actions: list[str]) -> str | None:
    records = _native_records(payload)
    starts = [index for index, record in enumerate(records) if record["type"] == "event_msg"
              and record["payload"].get("type") == "task_started"]
    if not starts or records[starts[-1]]["payload"].get("turn_id") != payload["turn_id"]:
        raise ValueError("UNKNOWN originating user turn: no longer current")
    if sum(records[index]["payload"].get("turn_id") == payload["turn_id"] for index in starts) != 1:
        raise ValueError("UNKNOWN originating user turn: duplicate start")
    current = records[starts[-1]:]
    if any(record["type"] == "event_msg" and record["payload"].get("type") in {
        "turn_aborted", "task_complete",
    } for record in current):
        return None
    text = _user_text(current, payload["turn_id"])
    if actions and (CONTINUATION_CHECK.fullmatch(text)
                    or text == "." and _interrupted_check(records, payload["turn_id"])):
        return actions[0]
    for match in ACTIVE_ASSIGNMENT.finditer(text):
        for action in actions:
            _, separator, instruction = action.partition(": ")
            if not separator or not instruction or not text.startswith(instruction, match.end()):
                continue
            remainder = text[:match.start()] + text[match.end() + len(instruction):]
            clauses = re.split(r"(?<=[.!])\s+|\n+", remainder.strip())
            if any(clause.lower().startswith("no production work") for clause in clauses) \
                    and not _isolated_file_action(payload, records, instruction, clauses):
                continue
            if all(not clause or AUXILIARY.fullmatch(clause) for clause in clauses):
                return action
    return None
