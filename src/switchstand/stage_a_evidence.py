"""Safe projection and validation for retained native-tree probe evidence."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .agent_tree import AgentTreeAdapter, THREAD_SOURCE_KINDS


SCHEMA_VERSION = 1
_SIMPLE_SUBAGENT_KINDS = frozenset(
    {"review", "compact", "thread_spawn", "other", "unknown"}
)


class ProbeEvidenceError(RuntimeError):
    """Raised when the requested live evidence is not available."""

    def __init__(self, code: str, message: str, *, phase: str) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase


class ProbeExecutionError(RuntimeError):
    """Sanitized probe failure with retained side-effect disclosure."""

    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int,
        side_effects: Mapping[str, Any],
        *,
        phase: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.side_effects = dict(side_effects)
        self.phase = phase


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_evidence(value: Any, *, expected_parent_id: Any) -> Any:
    """Project only explicitly approved native source fields and safe value shapes."""
    if isinstance(value, str):
        return value if value in THREAD_SOURCE_KINDS else "unknown"
    if not isinstance(value, Mapping) or not value:
        raise ProbeEvidenceError(
            "missing_native_source",
            "native source evidence is unavailable",
            phase="source_validation",
        )

    subagent = value.get("subAgent")
    if isinstance(subagent, str):
        kind = subagent if subagent in _SIMPLE_SUBAGENT_KINDS else "unknown"
        return {"subAgent": kind}
    if not isinstance(subagent, Mapping):
        return "unknown"

    spawn = subagent.get("thread_spawn")
    if not isinstance(spawn, Mapping):
        return {"subAgent": "unknown"}

    projected: dict[str, Any] = {}
    parent_id = spawn.get("parent_thread_id")
    if isinstance(parent_id, str) and parent_id and parent_id == expected_parent_id:
        projected["parent_thread_id"] = parent_id
    depth = spawn.get("depth")
    if isinstance(depth, int) and not isinstance(depth, bool) and depth >= 0:
        projected["depth"] = depth
    if "agent_path" in spawn:
        projected["agent_path"] = "[redacted]"
    return {"subAgent": {"thread_spawn": projected}}


def _status_evidence(status: Mapping[str, Any]) -> dict[str, Any]:
    """Project native status to the only fields Switchstand has validated."""
    projected = {"type": status["type"]}
    if status["type"] == "active":
        projected["activeFlags"] = list(status["activeFlags"])
    return projected


def _thread_evidence(thread: Mapping[str, Any]) -> dict[str, Any]:
    timestamps: dict[str, int | float] = {}
    for field in ("createdAt", "updatedAt"):
        value = thread.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProbeEvidenceError(
                "missing_protocol_timestamp",
                "a required protocol timestamp is unavailable",
                phase="timestamp_validation",
            )
        timestamps[field] = value
    return {
        "id": thread["id"],
        "parentThreadId": thread.get("parentThreadId"),
        "sessionId": thread["sessionId"],
        "source": _source_evidence(
            thread.get("source"),
            expected_parent_id=thread.get("parentThreadId"),
        ),
        "status": _status_evidence(thread["status"]),
        **timestamps,
    }


def snapshot(
    adapter: AgentTreeAdapter,
    root_thread_id: str,
    now: Callable[[], str],
) -> dict[str, Any]:
    started_at = now()
    tree = adapter.observe_tree(root_thread_id)
    completed_at = now()
    threads = [_thread_evidence(thread) for thread in tree["threads"]]
    if len(threads) < 2:
        raise ProbeEvidenceError(
            "no_spawned_descendant",
            "no spawned descendant was observed",
            phase="lineage_validation",
        )
    pages = tree.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ProbeEvidenceError(
            "missing_pagination_evidence",
            "descendant pagination evidence is unavailable",
            phase="descendant_list",
        )
    if pages[-1].get("nextCursor") is not None:
        raise ProbeEvidenceError(
            "incomplete_pagination",
            "descendant pagination was not exhausted",
            phase="descendant_list",
        )
    if any(page.get("sourceKinds") != list(THREAD_SOURCE_KINDS) for page in pages):
        raise ProbeEvidenceError(
            "incomplete_source_kind_coverage",
            "not every descendant page requested all source kinds",
            phase="descendant_list",
        )
    return {
        "observationWindow": {"startedAt": started_at, "completedAt": completed_at},
        "rootThreadId": tree["rootThreadId"],
        "sourceKindsRequested": tree["sourceKinds"],
        "pagination": {
            "complete": tree["paginationComplete"],
            "pagesRead": tree["pagesRead"],
            "pages": pages,
        },
        "threads": threads,
    }


def collect_notification(
    adapter: AgentTreeAdapter,
    message: Mapping[str, Any],
    observed_thread_ids: set[str],
    now: Callable[[], str],
) -> dict[str, Any] | None:
    if message.get("method") != "thread/status/changed":
        return None
    change = adapter.status_change(message)
    return {
        "receivedAt": now(),
        "threadId": change["threadId"],
        "status": _status_evidence(change["status"]),
        "belongsToObservedTree": change["threadId"] in observed_thread_ids,
    }
