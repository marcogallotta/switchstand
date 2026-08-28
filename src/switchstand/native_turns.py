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
    requested_terminal_status: Literal["completed", "failed", "interrupted"] | None = None


@dataclass(frozen=True)
class ExactTurnProjection:
    """Content-free newest-turn evidence for one privately rebound target."""

    status: Literal["none", "inProgress", "completed", "failed", "interrupted"]
    turn_id: str | None


ThreadStatus = Literal["active", "idle", "systemError", "notLoaded"]


def project_native_turns(
    response: Any,
    expected_target: object,
    *,
    terminal_turn_id: object | None = None,
) -> NativeTurnProjection | None:
    """Fail closed unless *response* is one consistent exact-target projection."""
    try:
        return _project_native_turns(response, expected_target, terminal_turn_id)
    except Exception:
        return None


def _project_native_turns(
    response: Any,
    expected_target: object,
    terminal_turn_id: object | None,
) -> NativeTurnProjection | None:
    if terminal_turn_id is not None and (
        type(terminal_turn_id) is not str
        or not terminal_turn_id
        or len(terminal_turn_id) > MAX_TURN_ID_CHARACTERS
    ):
        return None
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
    requested_terminal_status: Literal["completed", "failed", "interrupted"] | None = None
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
        elif turn_id == terminal_turn_id:
            requested_terminal_status = cast(
                Literal["completed", "failed", "interrupted"], turn_status
            )
    if (status == "active") != (active_turn_id is not None):
        return None
    return NativeTurnProjection(
        status=cast(ThreadStatus, status),
        active_turn_id=active_turn_id,
        requested_terminal_status=requested_terminal_status,
    )


def project_exact_turn_list(
    response: Any,
    expected_target: object,
) -> ExactTurnProjection | None:
    """Fail closed on anything except one content-free newest-turn result."""
    try:
        if not isinstance(response, Mapping) or response.get("target") != expected_target:
            return None
        if set(response) - {"target", "data", "nextCursor", "backwardsCursor"}:
            return None
        for cursor_name in ("nextCursor", "backwardsCursor"):
            cursor = response.get(cursor_name)
            if cursor_name in response and cursor is not None and (
                type(cursor) is not str or not cursor
            ):
                return None
        data = response.get("data")
        if not isinstance(data, list) or len(data) > 1:
            return None
        if not data:
            if response.get("nextCursor") is not None or response.get("backwardsCursor") is not None:
                return None
            return ExactTurnProjection("none", None)
        turn = data[0]
        if not isinstance(turn, Mapping):
            return None
        if set(turn) - {
            "id", "status", "items", "itemsView", "startedAt", "completedAt",
            "durationMs", "error",
        }:
            return None
        turn_id, status = turn.get("id"), turn.get("status")
        if (
            type(turn_id) is not str
            or not turn_id
            or len(turn_id) > MAX_TURN_ID_CHARACTERS
            or type(status) is not str
            or status not in TURN_STATUSES
            or turn.get("itemsView") != "notLoaded"
            or turn.get("items") != []
        ):
            return None
        return ExactTurnProjection(cast(Any, status), turn_id)
    except Exception:
        return None
