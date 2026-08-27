"""Safe projection and validation for retained native-tree probe evidence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .agent_tree import AgentTreeAdapter, THREAD_SOURCE_KINDS


SCHEMA_VERSION = 2
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


class EvidenceIdentifiers:
    """Assign stable run-local references without retaining native identifiers."""

    def __init__(self) -> None:
        self._thread_refs: dict[str, str] = {}
        self._session_refs: dict[str, str] = {}

    def thread_ref(self, thread_id: str | None) -> str | None:
        if thread_id is None:
            return None
        if thread_id not in self._thread_refs:
            self._thread_refs[thread_id] = f"thread-{len(self._thread_refs) + 1}"
        return self._thread_refs[thread_id]

    def session_ref(self, session_id: str) -> str:
        if session_id not in self._session_refs:
            self._session_refs[session_id] = f"session-{len(self._session_refs) + 1}"
        return self._session_refs[session_id]


@dataclass(frozen=True)
class SnapshotCapture:
    evidence: dict[str, Any]
    exact_threads: tuple[dict[str, Any], ...]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_evidence(value: Any) -> Any:
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


def _thread_evidence(
    thread: Mapping[str, Any],
    identifiers: EvidenceIdentifiers,
) -> dict[str, Any]:
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
        "threadRef": identifiers.thread_ref(thread["id"]),
        "parentThreadRef": identifiers.thread_ref(thread.get("parentThreadId")),
        "sessionRef": identifiers.session_ref(thread["sessionId"]),
        "source": _source_evidence(thread.get("source")),
        "status": _status_evidence(thread["status"]),
        **timestamps,
    }


def snapshot(
    adapter: AgentTreeAdapter,
    root_thread_id: str,
    now: Callable[[], str],
    identifiers: EvidenceIdentifiers,
) -> SnapshotCapture:
    started_at = now()
    tree = adapter.observe_tree(root_thread_id)
    completed_at = now()
    exact_threads = tuple(tree["threads"])
    threads = [_thread_evidence(thread, identifiers) for thread in exact_threads]
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
    retained_pages = [
        {
            "page": page["page"],
            "requestCursorPresent": page.get("requestCursor") is not None,
            "resultCount": page["resultCount"],
            "nextCursorPresent": page.get("nextCursor") is not None,
            "sourceKinds": page["sourceKinds"],
        }
        for page in pages
    ]
    evidence = {
        "observationWindow": {"startedAt": started_at, "completedAt": completed_at},
        "rootThreadRef": identifiers.thread_ref(tree["rootThreadId"]),
        "sourceKindsRequested": tree["sourceKinds"],
        "pagination": {
            "complete": tree["paginationComplete"],
            "pagesRead": tree["pagesRead"],
            "pages": retained_pages,
        },
        "threads": threads,
    }
    return SnapshotCapture(evidence=evidence, exact_threads=exact_threads)


def collect_notification(
    adapter: AgentTreeAdapter,
    message: Mapping[str, Any],
    observed_thread_ids: set[str],
    now: Callable[[], str],
    identifiers: EvidenceIdentifiers,
) -> tuple[dict[str, Any] | None, bool]:
    if message.get("method") != "thread/status/changed":
        return None, False
    change = adapter.status_change(message)
    if change["threadId"] not in observed_thread_ids:
        return None, True
    return {
        "receivedAt": now(),
        "threadRef": identifiers.thread_ref(change["threadId"]),
        "status": _status_evidence(change["status"]),
        "belongsToObservedTree": True,
    }, False
