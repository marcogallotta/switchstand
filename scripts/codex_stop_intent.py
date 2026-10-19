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
    r"this first (?:reply|turn) must precede the (?:remaining|pending) work|"
    r"on automatic continuation, (?:perform|continue|resume) the authorized "
    r"[a-z]+ (?:task|work|assignment) and verify it|"
    r"you may use the normal available file tools|"
    r"no production work or external services are authorized)\.?", re.IGNORECASE,
)


def _user_text(payload: dict[str, Any]) -> str:
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
        records = handle.read(TAIL_BYTES).splitlines()
    matches: list[str] = []
    message_ids: set[str] = set()
    started = False
    for line in records:
        record = json.loads(line)
        if not isinstance(record, dict):
            raise TypeError("UNKNOWN originating user turn: malformed source record")
        item = record.get("payload", {})
        if not isinstance(item, dict):
            raise TypeError("UNKNOWN originating user turn: malformed source payload")
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
            continue
        kinds = metadata.get("content_item_kinds")
        if kinds == ["user.text"]:
            retained = record.get("metadata", {}).get("retained_source", {})
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
    if not matches:
        raise ValueError("UNKNOWN originating user turn: absent user.text")
    return "\n".join(matches)


def affirmed_action(payload: dict[str, Any], actions: list[str]) -> str | None:
    text = _user_text(payload)
    for match in ACTIVE_ASSIGNMENT.finditer(text):
        for action in actions:
            _, separator, instruction = action.partition(": ")
            if not separator or not instruction or not text.startswith(instruction, match.end()):
                continue
            remainder = text[:match.start()] + text[match.end() + len(instruction):]
            clauses = re.split(r"(?<=[.!])\s+|\n+", remainder.strip())
            if all(not clause or AUXILIARY.fullmatch(clause) for clause in clauses):
                return action
    return None
