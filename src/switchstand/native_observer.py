"""Read-only, privacy-bounded observation of one exact native agent tree."""
from __future__ import annotations

from copy import deepcopy
import math
import threading
import time
from typing import Any, Callable, Mapping, Protocol, cast

from .agent_tree import AgentTreeAdapter, THREAD_SOURCE_KINDS


ERROR_CODE = "native_observation_unavailable"
_SUBAGENT_KINDS = frozenset({"review", "compact", "thread_spawn", "other", "unknown"})


class ObserverClient(Protocol):
    def thread_read(self, thread_id: str, *, include_turns: bool = True) -> Mapping[str, Any]: ...
    def thread_list(self, params: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def close(self) -> None: ...


class _StateDbOnlyClient:
    """Narrow the existing adapter to non-loading App Server reads."""

    def __init__(self, client: ObserverClient) -> None:
        self.client = client

    def thread_read(self, thread_id: str, *, include_turns: bool = True) -> Mapping[str, Any]:
        return self.client.thread_read(thread_id, include_turns=include_turns)

    def thread_list(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request = dict(params)
        request["useStateDbOnly"] = True
        return self.client.thread_list(request)


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timestamp unavailable")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("timestamp unavailable")
    return result


def _source(value: Any) -> str:
    if isinstance(value, str):
        return value if value in THREAD_SOURCE_KINDS else "unknown"
    if not isinstance(value, Mapping):
        return "unknown"
    subagent = value.get("subAgent")
    if isinstance(subagent, str):
        kind = subagent if subagent in _SUBAGENT_KINDS else "unknown"
        return f"subAgent:{kind}"
    if isinstance(subagent, Mapping) and isinstance(subagent.get("thread_spawn"), Mapping):
        return "subAgent:thread_spawn"
    return "unknown"


def _status(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {"type": value["type"]}
    if value["type"] == "active":
        result["activeFlags"] = list(value["activeFlags"])
    return result


class NativeObserver:
    """Poll complete native-tree passes without loading or mutating threads."""

    def __init__(
        self,
        client_factory: Callable[[], ObserverClient],
        root_thread_id: str,
        *,
        clock: Callable[[], float] = time.time,
        trail_limit: int = 20,
    ) -> None:
        if not root_thread_id:
            raise ValueError("an exact native root thread id is required")
        self._client_factory = client_factory
        self._root_thread_id = root_thread_id
        self._clock = clock
        self._trail_limit = max(1, trail_limit)
        self._lock = threading.Lock()
        self._refs: dict[str, str] = {}
        self._active_since: dict[str, float] = {}
        self._threads: list[dict[str, Any]] = []
        self._trail: list[dict[str, Any]] = []
        self._pass_sequence = 0
        self._state: dict[str, Any] = {
            "mode": "native",
            "passSequence": 0,
            "observation": {
                "connected": False,
                "historical": False,
                "errorCode": None,
                "completedAt": None,
                "kind": "completed multi-request observation pass",
                "caveat": "Polling may miss intermediate endpoint transitions.",
            },
            "threads": [],
            "differences": [],
        }

    def _ref(self, thread_id: str, refs: dict[str, str] | None = None) -> str:
        target = self._refs if refs is None else refs
        if thread_id not in target:
            target[thread_id] = f"thread-{len(target) + 1}"
        return target[thread_id]

    def _project(self, tree: Mapping[str, Any], completed_at: float) -> list[dict[str, Any]]:
        raw_threads = tree["threads"]
        if not isinstance(raw_threads, list) or not raw_threads:
            raise ValueError("tree unavailable")
        refs = dict(self._refs)
        for thread in raw_threads:
            self._ref(str(thread["id"]), refs)
        projected = []
        active_now: dict[str, float] = {}
        for thread in raw_threads:
            thread_id = str(thread["id"])
            ref = self._ref(thread_id, refs)
            parent_id = thread.get("parentThreadId")
            parent_ref = self._ref(parent_id, refs) if isinstance(parent_id, str) else None
            status = _status(thread["status"])
            active_observed_seconds = None
            if status["type"] == "active":
                active_now[ref] = self._active_since.get(ref, completed_at)
                active_observed_seconds = max(0.0, completed_at - active_now[ref])
            projected.append(
                {
                    "ref": ref,
                    "label": "Root" if parent_ref is None else f"Agent {int(ref.split('-')[1]) - 1}",
                    "parentRef": parent_ref,
                    "depth": self._depth(thread_id, raw_threads),
                    "source": _source(thread.get("source")),
                    "createdAt": _timestamp(thread.get("createdAt")),
                    "updatedAt": _timestamp(thread.get("updatedAt")),
                    "status": status,
                    "activeObservedSeconds": active_observed_seconds,
                }
            )
        self._active_since = active_now
        self._refs = refs
        return projected

    @staticmethod
    def _depth(thread_id: str, threads: list[dict[str, Any]]) -> int:
        parents = {str(item["id"]): item.get("parentThreadId") for item in threads}
        depth = 0
        parent = parents[thread_id]
        while isinstance(parent, str):
            depth += 1
            parent = parents[parent]
        return depth

    def _record_differences(self, previous: list[dict[str, Any]], current: list[dict[str, Any]], at: float) -> None:
        if not previous:
            return
        old = {item["ref"]: item for item in previous}
        new = {item["ref"]: item for item in current}
        for ref in sorted(set(old) | set(new)):
            for field in ("status", "updatedAt", "parentRef", "source"):
                before = old.get(ref, {}).get(field, "unobserved")
                after = new.get(ref, {}).get(field, "unobserved")
                if before != after:
                    self._trail.append(
                        {"observedAt": at, "threadRef": ref, "field": field, "before": before, "after": after}
                    )
        self._trail = self._trail[-self._trail_limit :]

    def observe_once(self) -> None:
        client: ObserverClient | None = None
        try:
            client = self._client_factory()
            tree = AgentTreeAdapter(cast(Any, _StateDbOnlyClient(client))).observe_tree(
                self._root_thread_id
            )
            completed_at = self._clock()
            with self._lock:
                threads = self._project(tree, completed_at)
                self._record_differences(self._threads, threads, completed_at)
                self._threads = threads
                self._pass_sequence += 1
                self._state = {
                    "mode": "native",
                    "passSequence": self._pass_sequence,
                    "observation": {
                        "connected": True,
                        "historical": False,
                        "errorCode": None,
                        "completedAt": completed_at,
                        "kind": "completed multi-request observation pass",
                        "caveat": "Polling may miss intermediate endpoint transitions.",
                    },
                    "threads": threads,
                    "differences": list(self._trail),
                }
        except Exception:
            with self._lock:
                self._active_since.clear()
                observation = dict(self._state["observation"])
                observation.update(connected=False, historical=bool(self._threads), errorCode=ERROR_CODE)
                self._state = {**self._state, "observation": observation}
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)
