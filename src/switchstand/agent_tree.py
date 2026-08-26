"""Fail-closed observation and control for the native Codex agent tree.

This module is deliberately separate from the synthetic two-role attempt engine.
It does not persist, rename, replace, or reinterpret native Codex threads.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Protocol


# Passing no sourceKinds (or an empty list) only returns interactive sources.
# Keep this list explicit so a complete pagination pass can include every
# documented root, subagent, and unknown source classification.
THREAD_SOURCE_KINDS = (
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
)
NATIVE_THREAD_STATUS_TYPES = frozenset({"active", "idle", "systemError", "notLoaded"})
NATIVE_ACTIVE_FLAGS = frozenset({"waitingOnApproval", "waitingOnUserInput"})


class AgentTreeEvidenceError(RuntimeError):
    """Raised when App Server evidence is incomplete or internally inconsistent."""

    def __init__(self, code: str, message: str, *, phase: str) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase


class AgentTreeClient(Protocol):
    def thread_read(self, thread_id: str, *, include_turns: bool = True) -> Mapping[str, Any]: ...
    def thread_list(self, params: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def thread_resume(self, thread_id: str) -> Mapping[str, Any]: ...
    def turn_start_text_native(self, thread_id: str, text: str) -> Mapping[str, Any]: ...
    def turn_steer_text(
        self, thread_id: str, expected_turn_id: str, text: str
    ) -> Mapping[str, Any]: ...
    def turn_interrupt(self, thread_id: str, turn_id: str) -> Mapping[str, Any]: ...


def _required_text(
    value: Mapping[str, Any],
    field: str,
    *,
    context: str,
    code: str = "missing_required_field",
    phase: str = "protocol_validation",
) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise AgentTreeEvidenceError(code, "required protocol identity is unavailable", phase=phase)
    return result


def validate_native_status(
    value: Any, *, context: str, phase: str = "status_validation"
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentTreeEvidenceError(
            "invalid_native_status", "native runtime status is unavailable", phase=phase
        )
    status_type = value.get("type")
    if status_type not in NATIVE_THREAD_STATUS_TYPES:
        raise AgentTreeEvidenceError(
            "unsupported_status_or_flag",
            "native runtime status or active flag is unsupported",
            phase=phase,
        )
    if status_type == "active":
        flags = value.get("activeFlags")
        if not isinstance(flags, list) or any(flag not in NATIVE_ACTIVE_FLAGS for flag in flags):
            raise AgentTreeEvidenceError(
                "unsupported_status_or_flag",
                "native runtime status or active flag is unsupported",
                phase=phase,
            )
    return deepcopy(dict(value))


def validate_thread(
    value: Any,
    *,
    context: str,
    phase: str = "protocol_validation",
    invalid_code: str = "invalid_thread_record",
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentTreeEvidenceError(
            invalid_code, "native thread evidence is unavailable or invalid", phase=phase
        )
    thread = deepcopy(dict(value))
    thread_id = _required_text(
        thread, "id", context=context, code=invalid_code, phase=phase
    )
    _required_text(
        thread, "sessionId", context=f"thread {thread_id}", code=invalid_code, phase=phase
    )
    parent_id = thread.get("parentThreadId")
    if parent_id is not None and (not isinstance(parent_id, str) or not parent_id):
        raise AgentTreeEvidenceError(
            invalid_code, "native thread evidence is unavailable or invalid", phase=phase
        )
    validate_native_status(thread.get("status"), context=f"thread {thread_id}", phase=phase)
    return thread


class AgentTreeAdapter:
    """Read native thread truth without adapting it into Switchstand roles."""

    def __init__(self, client: AgentTreeClient) -> None:
        self.client = client

    def _list_all(
        self, params: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        threads: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        while True:
            request = dict(params)
            request["sourceKinds"] = list(THREAD_SOURCE_KINDS)
            request["limit"] = 100
            if cursor is not None:
                request["cursor"] = cursor
            response = self.client.thread_list(request)
            page_number = len(pages) + 1
            data = response.get("data") if isinstance(response, Mapping) else None
            if not isinstance(data, list):
                raise AgentTreeEvidenceError(
                    "invalid_pagination",
                    "descendant pagination evidence is invalid or incomplete",
                    phase="descendant_list",
                )
            threads.extend(
                validate_thread(
                    item,
                    context=f"thread/list page {page_number}",
                    phase="descendant_list",
                    invalid_code="invalid_descendant_record",
                )
                for item in data
            )
            next_cursor = response.get("nextCursor")
            pages.append(
                {
                    "page": page_number,
                    "requestCursor": cursor,
                    "resultCount": len(data),
                    "nextCursor": next_cursor,
                    "sourceKinds": list(THREAD_SOURCE_KINDS),
                }
            )
            if next_cursor is None:
                return threads, pages
            if not isinstance(next_cursor, str) or not next_cursor:
                raise AgentTreeEvidenceError(
                    "invalid_pagination",
                    "descendant pagination evidence is invalid or incomplete",
                    phase="descendant_list",
                )
            if next_cursor in seen_cursors:
                raise AgentTreeEvidenceError(
                    "invalid_pagination",
                    "descendant pagination evidence is invalid or incomplete",
                    phase="descendant_list",
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def observe_tree(self, root_thread_id: str) -> dict[str, Any]:
        """Return one root and every paginated spawned descendant.

        Spawned lineage is accepted only from parentThreadId. forkedFromId is
        preserved as raw protocol evidence but never used to construct ancestry.
        """
        root_response = self.client.thread_read(root_thread_id, include_turns=False)
        root_raw = root_response.get("thread") if isinstance(root_response, Mapping) else None
        root = validate_thread(
            root_raw,
            context="thread/read root",
            phase="root_read",
            invalid_code="root_not_found_or_invalid",
        )
        if root["id"] != root_thread_id:
            raise AgentTreeEvidenceError(
                "root_not_found_or_invalid",
                "the requested root was not returned as valid native evidence",
                phase="root_read",
            )
        if root.get("parentThreadId") is not None:
            raise AgentTreeEvidenceError(
                "selected_thread_not_root",
                "the selected thread is not a native root",
                phase="root_read",
            )
        root_session_id = str(root["sessionId"])
        if root_session_id != root_thread_id:
            raise AgentTreeEvidenceError(
                "root_session_mismatch",
                "the selected root does not own its native session tree",
                phase="root_read",
            )

        descendants, pages = self._list_all({"ancestorThreadId": root_thread_id})
        by_id = {root_thread_id: root}
        for thread in descendants:
            thread_id = str(thread["id"])
            if thread_id in by_id:
                raise AgentTreeEvidenceError(
                    "duplicate_thread",
                    "descendant evidence contains a duplicate thread",
                    phase="descendant_list",
                )
            by_id[thread_id] = thread

        for thread in descendants:
            thread_id = str(thread["id"])
            if thread.get("sessionId") != root_session_id:
                raise AgentTreeEvidenceError(
                    "descendant_session_mismatch",
                    "a descendant does not share the root native session",
                    phase="lineage_validation",
                )
            parent_id = thread.get("parentThreadId")
            if not isinstance(parent_id, str) or not parent_id:
                raise AgentTreeEvidenceError(
                    "missing_parent_edge",
                    "a descendant has no valid spawned-parent edge",
                    phase="lineage_validation",
                )
            visited = {thread_id}
            while parent_id != root_thread_id:
                if parent_id in visited:
                    raise AgentTreeEvidenceError(
                        "lineage_cycle",
                        "spawned lineage contains a cycle",
                        phase="lineage_validation",
                    )
                visited.add(parent_id)
                parent = by_id.get(parent_id)
                if parent is None:
                    raise AgentTreeEvidenceError(
                        "missing_intermediate_parent",
                        "spawned lineage is missing an intermediate parent",
                        phase="lineage_validation",
                    )
                parent_id = parent.get("parentThreadId")
                if not isinstance(parent_id, str) or not parent_id:
                    raise AgentTreeEvidenceError(
                        "missing_parent_edge",
                        "spawned lineage does not reach the selected root",
                        phase="lineage_validation",
                    )

        return {
            "rootThreadId": root_thread_id,
            "sourceKinds": list(THREAD_SOURCE_KINDS),
            "pagesRead": len(pages),
            "pages": pages,
            "paginationComplete": True,
            "threads": [root, *descendants],
        }

    def status_change(self, notification: Any) -> dict[str, Any]:
        if not isinstance(notification, Mapping) or notification.get("method") != "thread/status/changed":
            raise AgentTreeEvidenceError(
                "invalid_status_notification",
                "status-change notification evidence is invalid",
                phase="notification_validation",
            )
        params = notification.get("params")
        if not isinstance(params, Mapping):
            raise AgentTreeEvidenceError(
                "invalid_status_notification",
                "status-change notification evidence is invalid",
                phase="notification_validation",
            )
        thread_id = _required_text(
            params,
            "threadId",
            context="status notification",
            code="invalid_status_notification",
            phase="notification_validation",
        )
        status = validate_native_status(
            params.get("status"),
            context=f"thread {thread_id}",
            phase="notification_validation",
        )
        return {"threadId": thread_id, "status": status}

    def resume_exact(self, thread_id: str) -> dict[str, Any]:
        """Load and subscribe to one exact thread without starting a turn.

        This changes App Server runtime loaded/subscription state. It does not
        add conversation history, but callers must opt in to the consequence.
        """
        response = self.client.thread_resume(thread_id)
        thread_raw = response.get("thread") if isinstance(response, Mapping) else None
        if not isinstance(thread_raw, Mapping):
            raise AgentTreeEvidenceError(
                "resume_ack_invalid",
                "thread resume acknowledgement is unavailable or invalid",
                phase="subscription_setup",
            )
        resumed_id = _required_text(
            thread_raw,
            "id",
            context="thread/resume response",
            code="resume_ack_invalid",
            phase="subscription_setup",
        )
        if resumed_id != thread_id:
            raise AgentTreeEvidenceError(
                "resume_ack_mismatch",
                "thread resume did not acknowledge the exact requested thread",
                phase="subscription_setup",
            )
        return deepcopy(dict(thread_raw))

    def send_text(self, thread_id: str, text: str) -> dict[str, str]:
        """Start on native idle, or steer the exact active turn.

        The read is an observation, not a lock. App Server remains authoritative:
        a concurrent status change must make the exact native request fail rather
        than being retried through another mode.
        """
        text = str(text).strip()
        if not text:
            raise ValueError("message text is required")
        response = self.client.thread_read(thread_id, include_turns=True)
        thread_raw = response.get("thread") if isinstance(response, Mapping) else None
        thread = validate_thread(thread_raw, context="thread/read send target")
        if thread["id"] != thread_id:
            raise AgentTreeEvidenceError(
                "send_target_mismatch",
                "thread read did not return the exact send target",
                phase="control_validation",
            )
        status = validate_native_status(thread.get("status"), context=f"thread {thread_id}")
        if status["type"] == "idle":
            started = self.client.turn_start_text_native(thread_id, text)
            turn = started.get("turn") if isinstance(started, Mapping) else None
            turn_id = _required_text(turn, "id", context="turn/start response") if isinstance(turn, Mapping) else ""
            if not turn_id:
                raise AgentTreeEvidenceError(
                    "turn_ack_invalid",
                    "turn start did not return an exact turn acknowledgement",
                    phase="control_validation",
                )
            return {"mode": "start", "threadId": thread_id, "turnId": turn_id}
        if status["type"] == "active":
            turns = thread.get("turns")
            if not isinstance(turns, list):
                raise AgentTreeEvidenceError(
                    "active_turn_unavailable",
                    "active thread history does not expose an exact turn",
                    phase="control_validation",
                )
            active_ids = [
                item.get("id")
                for item in turns
                if isinstance(item, Mapping)
                and item.get("status") == "inProgress"
                and isinstance(item.get("id"), str)
                and item.get("id")
            ]
            if len(active_ids) != 1:
                raise AgentTreeEvidenceError(
                    "active_turn_unavailable",
                    "active thread history does not expose an exact turn",
                    phase="control_validation",
                )
            expected_turn_id = str(active_ids[0])
            steered = self.client.turn_steer_text(thread_id, expected_turn_id, text)
            accepted_turn_id = (
                steered.get("turnId") if isinstance(steered, Mapping) else None
            )
            if accepted_turn_id != expected_turn_id:
                raise AgentTreeEvidenceError(
                    "turn_ack_mismatch",
                    "turn steer did not acknowledge the exact expected turn",
                    phase="control_validation",
                )
            return {"mode": "steer", "threadId": thread_id, "turnId": expected_turn_id}
        raise AgentTreeEvidenceError(
            "direct_input_unavailable",
            "native thread status does not permit direct input",
            phase="control_validation",
        )

    def stop(self, thread_id: str, turn_id: str) -> None:
        if not thread_id or not turn_id:
            raise ValueError("exact thread and turn ids are required")
        self.client.turn_interrupt(thread_id, turn_id)
