"""Strict, content-discarding projection of native thread and turn state."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, cast


THREAD_STATUSES = frozenset({"active", "idle", "systemError", "notLoaded"})
TURN_STATUSES = frozenset({"inProgress", "completed", "failed", "interrupted"})
MAX_TURNS = 256
MAX_TURN_ID_CHARACTERS = 256


@dataclass(frozen=True)
class NativeTurnProjection:
    """The only thread/turn facts an exact native action may consume."""

    status: Literal["active", "idle", "systemError", "notLoaded"]
    active_turn_id: str | None


ThreadStatus = Literal["active", "idle", "systemError", "notLoaded"]


def project_native_turns(
    response: Any, expected_target: object
) -> NativeTurnProjection | None:
    """Fail closed unless *response* is one consistent exact-target projection."""
    try:
        return _project_native_turns(response, expected_target)
    except Exception:
        return None


def _project_native_turns(
    response: Any, expected_target: object
) -> NativeTurnProjection | None:
    if not isinstance(response, Mapping):
        return None
    thread = response.get("thread")
    if not isinstance(thread, Mapping) or thread.get("id") != expected_target:
        return None
    status_value = thread.get("status")
    if not isinstance(status_value, Mapping):
        return None
    status = status_value.get("type")
    if type(status) is not str or status not in THREAD_STATUSES:
        return None
    turns = thread.get("turns")
    if not isinstance(turns, list) or len(turns) > MAX_TURNS:
        return None
    seen: set[str] = set()
    active_turn_id: str | None = None
    for turn in turns:
        if not isinstance(turn, Mapping):
            return None
        turn_id = turn.get("id")
        turn_status = turn.get("status")
        if (
            type(turn_id) is not str
            or not turn_id
            or len(turn_id) > MAX_TURN_ID_CHARACTERS
            or turn_id in seen
            or type(turn_status) is not str
            or turn_status not in TURN_STATUSES
        ):
            return None
        seen.add(turn_id)
        if turn_status == "inProgress":
            if active_turn_id is not None:
                return None
            active_turn_id = turn_id
    if (status == "active") != (active_turn_id is not None):
        return None
    return NativeTurnProjection(
        status=cast(ThreadStatus, status), active_turn_id=active_turn_id
    )
