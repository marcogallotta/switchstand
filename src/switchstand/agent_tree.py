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


class AgentTreeClient(Protocol):
    def thread_read(self, thread_id: str, *, include_turns: bool = True) -> Mapping[str, Any]: ...
    def thread_list(self, params: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def thread_resume(self, thread_id: str) -> Mapping[str, Any]: ...
    def turn_start_text_native(self, thread_id: str, text: str) -> Mapping[str, Any]: ...
    def turn_steer_text(
        self, thread_id: str, expected_turn_id: str, text: str
    ) -> Mapping[str, Any]: ...
    def turn_interrupt(self, thread_id: str, turn_id: str) -> Mapping[str, Any]: ...


def _required_text(value: Mapping[str, Any], field: str, *, context: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise AgentTreeEvidenceError(f"{context} has no usable {field}")
    return result


def validate_native_status(value: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentTreeEvidenceError(f"{context} has no native runtime status")
    status_type = value.get("type")
    if status_type not in NATIVE_THREAD_STATUS_TYPES:
        raise AgentTreeEvidenceError(f"{context} has unsupported native status {status_type!r}")
    if status_type == "active":
        flags = value.get("activeFlags")
        if not isinstance(flags, list) or any(flag not in NATIVE_ACTIVE_FLAGS for flag in flags):
            raise AgentTreeEvidenceError(f"{context} has unsupported active flags")
    return deepcopy(dict(value))


def validate_thread(value: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentTreeEvidenceError(f"{context} is not a thread object")
    thread = deepcopy(dict(value))
    thread_id = _required_text(thread, "id", context=context)
    _required_text(thread, "sessionId", context=f"thread {thread_id}")
    parent_id = thread.get("parentThreadId")
    if parent_id is not None and (not isinstance(parent_id, str) or not parent_id):
        raise AgentTreeEvidenceError(f"thread {thread_id} has invalid parentThreadId")
    validate_native_status(thread.get("status"), context=f"thread {thread_id}")
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
                raise AgentTreeEvidenceError("thread/list returned no data page")
            threads.extend(
                validate_thread(item, context=f"thread/list page {page_number}") for item in data
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
                raise AgentTreeEvidenceError("thread/list returned an invalid nextCursor")
            if next_cursor in seen_cursors:
                raise AgentTreeEvidenceError("thread/list pagination repeated a cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def observe_tree(self, root_thread_id: str) -> dict[str, Any]:
        """Return one root and every paginated spawned descendant.

        Spawned lineage is accepted only from parentThreadId. forkedFromId is
        preserved as raw protocol evidence but never used to construct ancestry.
        """
        root_response = self.client.thread_read(root_thread_id, include_turns=False)
        root_raw = root_response.get("thread") if isinstance(root_response, Mapping) else None
        root = validate_thread(root_raw, context="thread/read root")
        if root["id"] != root_thread_id:
            raise AgentTreeEvidenceError("thread/read returned a different root thread")
        if root.get("parentThreadId") is not None:
            raise AgentTreeEvidenceError("selected root has a spawned parent")
        root_session_id = str(root["sessionId"])
        if root_session_id != root_thread_id:
            raise AgentTreeEvidenceError("selected root does not own its native session tree")

        descendants, pages = self._list_all({"ancestorThreadId": root_thread_id})
        by_id = {root_thread_id: root}
        for thread in descendants:
            thread_id = str(thread["id"])
            if thread_id in by_id:
                raise AgentTreeEvidenceError(f"thread/list repeated thread {thread_id}")
            by_id[thread_id] = thread

        for thread in descendants:
            thread_id = str(thread["id"])
            if thread.get("sessionId") != root_session_id:
                raise AgentTreeEvidenceError(
                    f"descendant {thread_id} does not share root session {root_session_id}"
                )
            parent_id = thread.get("parentThreadId")
            if not isinstance(parent_id, str) or not parent_id:
                raise AgentTreeEvidenceError(f"descendant {thread_id} has no spawned parent")
            visited = {thread_id}
            while parent_id != root_thread_id:
                if parent_id in visited:
                    raise AgentTreeEvidenceError(f"spawned lineage for {thread_id} contains a cycle")
                visited.add(parent_id)
                parent = by_id.get(parent_id)
                if parent is None:
                    raise AgentTreeEvidenceError(
                        f"spawned lineage for {thread_id} is missing parent {parent_id}"
                    )
                parent_id = parent.get("parentThreadId")
                if not isinstance(parent_id, str) or not parent_id:
                    raise AgentTreeEvidenceError(
                        f"spawned lineage for {thread_id} does not reach root {root_thread_id}"
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
            raise AgentTreeEvidenceError("notification is not thread/status/changed")
        params = notification.get("params")
        if not isinstance(params, Mapping):
            raise AgentTreeEvidenceError("status notification has no params")
        thread_id = _required_text(params, "threadId", context="status notification")
        status = validate_native_status(params.get("status"), context=f"thread {thread_id}")
        return {"threadId": thread_id, "status": status}

    def resume_exact(self, thread_id: str) -> dict[str, Any]:
        """Load and subscribe to one exact thread without starting a turn.

        This changes App Server runtime loaded/subscription state. It does not
        add conversation history, but callers must opt in to the consequence.
        """
        response = self.client.thread_resume(thread_id)
        thread_raw = response.get("thread") if isinstance(response, Mapping) else None
        if not isinstance(thread_raw, Mapping):
            raise AgentTreeEvidenceError("thread/resume returned no thread")
        resumed_id = _required_text(thread_raw, "id", context="thread/resume response")
        if resumed_id != thread_id:
            raise AgentTreeEvidenceError("thread/resume returned a different thread")
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
            raise AgentTreeEvidenceError("thread/read returned a different send target")
        status = validate_native_status(thread.get("status"), context=f"thread {thread_id}")
        if status["type"] == "idle":
            started = self.client.turn_start_text_native(thread_id, text)
            turn = started.get("turn") if isinstance(started, Mapping) else None
            turn_id = _required_text(turn, "id", context="turn/start response") if isinstance(turn, Mapping) else ""
            if not turn_id:
                raise AgentTreeEvidenceError("turn/start returned no exact turn id")
            return {"mode": "start", "threadId": thread_id, "turnId": turn_id}
        if status["type"] == "active":
            turns = thread.get("turns")
            if not isinstance(turns, list):
                raise AgentTreeEvidenceError("active thread history has no turns")
            active_ids = [
                item.get("id")
                for item in turns
                if isinstance(item, Mapping)
                and item.get("status") == "inProgress"
                and isinstance(item.get("id"), str)
                and item.get("id")
            ]
            if len(active_ids) != 1:
                raise AgentTreeEvidenceError("active thread does not expose one exact in-progress turn")
            expected_turn_id = str(active_ids[0])
            steered = self.client.turn_steer_text(thread_id, expected_turn_id, text)
            accepted_turn_id = (
                steered.get("turnId") if isinstance(steered, Mapping) else None
            )
            if accepted_turn_id != expected_turn_id:
                raise AgentTreeEvidenceError("turn/steer did not acknowledge the exact expected turn")
            return {"mode": "steer", "threadId": thread_id, "turnId": expected_turn_id}
        raise AgentTreeEvidenceError(
            f"native thread {thread_id} is {status['type']}; direct input is unavailable"
        )

    def stop(self, thread_id: str, turn_id: str) -> None:
        if not thread_id or not turn_id:
            raise ValueError("exact thread and turn ids are required")
        self.client.turn_interrupt(thread_id, turn_id)
