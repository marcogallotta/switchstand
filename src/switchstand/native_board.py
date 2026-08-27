"""Read-only in-memory projection of one exact native Codex agent tree."""
from __future__ import annotations

from copy import deepcopy
import threading
import time
from typing import Any, Callable, Mapping, Protocol, cast

from .agent_tree import AgentTreeAdapter, AgentTreeClient, THREAD_SOURCE_KINDS
from .native_stop import NativeStop, StopClient


SAFE_ERROR = "native_observation_unavailable"
DISCLOSURE = (
    "Polling may miss intermediate transitions; trail entries are observed endpoint "
    "differences, not native events."
)
_SAFE_SUBAGENT_DETAILS = frozenset({"review", "compact", "thread_spawn", "other", "unknown"})


class NativeClient(Protocol):
    def thread_read(self, thread_id: str, *, include_turns: bool = True) -> Mapping[str, Any]: ...
    def thread_list(self, params: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def close(self) -> None: ...


class _StateDbOnlyClient:
    """Expose only reads and prohibit list calls from scanning transcript logs."""

    def __init__(self, client: NativeClient) -> None:
        self._client = client

    def thread_read(self, thread_id: str, *, include_turns: bool = True) -> Mapping[str, Any]:
        return self._client.thread_read(thread_id, include_turns=include_turns)

    def thread_list(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request = dict(params)
        request["useStateDbOnly"] = True
        return self._client.thread_list(request)


def _source(value: Any) -> tuple[str, str | None]:
    if isinstance(value, str):
        return (value if value in THREAD_SOURCE_KINDS else "unknown", None)
    if not isinstance(value, Mapping):
        return "unknown", None
    subagent = value.get("subAgent")
    if isinstance(subagent, str):
        return "subAgent", subagent if subagent in _SAFE_SUBAGENT_DETAILS else "unknown"
    if isinstance(subagent, Mapping) and isinstance(subagent.get("thread_spawn"), Mapping):
        return "subAgent", "thread_spawn"
    return "unknown", None


def _timestamp(thread: Mapping[str, Any], field: str) -> float:
    value = thread.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("native timestamp unavailable")
    return float(value)


class NativeBoard:
    """Poll complete native-tree passes and retain only a safe browser projection."""

    def __init__(
        self,
        client_factory: Callable[[], NativeClient],
        root_thread_id: str,
        *,
        poll_interval_seconds: float = 1.0,
        trail_limit: int = 50,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not root_thread_id:
            raise ValueError("an exact native root thread id is required")
        if poll_interval_seconds <= 0 or trail_limit <= 0:
            raise ValueError("poll interval and trail limit must be positive")
        self._client_factory = client_factory
        self._root_thread_id = root_thread_id
        self._poll_interval = poll_interval_seconds
        self._trail_limit = trail_limit
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._labels: dict[str, str] = {}
        self._active_since: dict[str, float] = {}
        self._endpoints: dict[str, dict[str, Any]] = {}
        self._agents: list[dict[str, Any]] = []
        self._trail: list[dict[str, Any]] = []
        self._completed_at: float | None = None
        self._connected = False
        self._error_code: str | None = None
        self._stopper = NativeStop(cast(Callable[[], StopClient], client_factory), self._resolve_active)

    def start(self) -> None:
        self.poll_once()
        self._thread = threading.Thread(target=self._run, name="switchstand-native-board", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval):
            self.poll_once()

    def _label(self, thread_id: str) -> str:
        if thread_id not in self._labels:
            self._labels[thread_id] = f"agent-{len(self._labels) + 1}"
        return self._labels[thread_id]

    def _project(self, threads: list[Mapping[str, Any]], completed_mono: float) -> list[dict[str, Any]]:
        by_id = {str(thread["id"]): thread for thread in threads}
        depth_cache: dict[str, int] = {}

        def depth(thread_id: str) -> int:
            if thread_id in depth_cache:
                return depth_cache[thread_id]
            parent = by_id[thread_id].get("parentThreadId")
            result = 0 if parent is None else depth(str(parent)) + 1
            depth_cache[thread_id] = result
            return result

        projected = []
        current_ids = set(by_id)
        self._active_since = {
            thread_id: started
            for thread_id, started in self._active_since.items()
            if thread_id in current_ids
        }
        for thread in threads:
            thread_id = str(thread["id"])
            ref = self._label(thread_id)
            parent_id = thread.get("parentThreadId")
            status = dict(thread["status"])
            status_type = str(status["type"])
            was_active = self._endpoints.get(thread_id, {}).get("status") == "active"
            if status_type == "active":
                if not was_active or thread_id not in self._active_since:
                    self._active_since[thread_id] = completed_mono
                active_seconds = completed_mono - self._active_since[thread_id]
            else:
                self._active_since.pop(thread_id, None)
                active_seconds = 0.0
            source_kind, source_detail = _source(thread.get("source"))
            projected.append(
                {
                    "agentRef": ref,
                    "label": f"Agent {ref.removeprefix('agent-')}",
                    "parentRef": self._label(str(parent_id)) if parent_id is not None else None,
                    "depth": depth(thread_id),
                    "sourceKind": source_kind,
                    "sourceDetail": source_detail,
                    "createdAt": _timestamp(thread, "createdAt"),
                    "updatedAt": _timestamp(thread, "updatedAt"),
                    "status": status_type,
                    "activeFlags": list(status.get("activeFlags", [])) if status_type == "active" else [],
                    "activeObservedSeconds": round(active_seconds, 3),
                }
            )
        return projected

    @staticmethod
    def _endpoint(agent: Mapping[str, Any]) -> dict[str, Any]:
        return {key: deepcopy(value) for key, value in agent.items() if key not in {"label", "activeObservedSeconds"}}

    def _record_differences(self, agents: list[dict[str, Any]], observed_at: float) -> None:
        current = {agent["agentRef"]: self._endpoint(agent) for agent in agents}
        previous = {self._label(thread_id): value for thread_id, value in self._endpoints.items()}
        entries = []
        for ref in sorted(set(previous) | set(current)):
            before, after = previous.get(ref), current.get(ref)
            if before is None or after is None:
                changes = {"presence": {"from": "absent" if before is None else "present", "to": "present" if after is not None else "absent"}}
            else:
                changes = {
                    key: {"from": before.get(key), "to": after.get(key)}
                    for key in sorted(set(before) | set(after))
                    if before.get(key) != after.get(key)
                }
            if changes:
                entries.append({"observedAt": observed_at, "agentRef": ref, "changes": changes})
        self._trail = (self._trail + entries)[-self._trail_limit :]
        id_by_ref = {self._label(thread_id): thread_id for thread_id in self._labels}
        self._endpoints = {id_by_ref[ref]: value for ref, value in current.items()}

    def poll_once(self) -> None:
        client: NativeClient | None = None
        try:
            client = self._client_factory()
            read_client = cast(AgentTreeClient, _StateDbOnlyClient(client))
            tree = AgentTreeAdapter(read_client).observe_tree(self._root_thread_id)
            completed_wall = self._wall_clock()
            completed_mono = self._monotonic()
            threads = list(tree["threads"])
            with self._lock:
                agents = self._project(threads, completed_mono)
                self._record_differences(agents, completed_wall)
                self._agents = agents
                self._completed_at = completed_wall
                self._connected = True
                self._error_code = None
        except Exception:
            with self._lock:
                self._connected = False
                self._error_code = SAFE_ERROR
                self._active_since.clear()
                for agent in self._agents:
                    agent["activeObservedSeconds"] = 0.0
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            completed_at = self._completed_at
            now = self._wall_clock()
            agents = deepcopy(self._agents)
            for agent in agents:
                agent["updatedAgeSeconds"] = round(max(0.0, now - agent["updatedAt"]), 3)
            return {
                "mode": "native",
                "observation": {
                    "connected": self._connected,
                    "available": self._connected and completed_at is not None,
                    "historical": not self._connected and completed_at is not None,
                    "errorCode": self._error_code,
                    "completedAt": completed_at,
                    "passAgeSeconds": None if completed_at is None else round(max(0.0, now - completed_at), 3),
                    "kind": "completed_multi_request_pass",
                },
                "agents": agents,
                "trail": deepcopy(self._trail),
                "trailLimit": self._trail_limit,
                "disclosure": DISCLOSURE,
            }

    def _resolve_active(self, agent_ref: str) -> str | None:
        with self._lock:
            if not self._connected:
                return None
            for thread_id, endpoint in self._endpoints.items():
                if self._labels.get(thread_id) == agent_ref and endpoint.get("status") == "active":
                    return thread_id
        return None

    def prepare_stop(self, agent_ref: Any) -> dict[str, str]:
        return self._stopper.prepare(agent_ref)

    def commit_stop(self, confirmation_ref: Any) -> dict[str, str]:
        return self._stopper.commit(confirmation_ref)

    def stop_status(self, operation_ref: Any) -> dict[str, str]:
        return self._stopper.status(operation_ref)
