"""Read-only in-memory projection of one exact native Codex agent tree."""
from __future__ import annotations

from copy import deepcopy
import math
import secrets
import threading
import time
from typing import Any, Callable, Mapping, Protocol, TypeVar, cast

from .agent_tree import AgentTreeAdapter, AgentTreeClient
from .current_target import (
    ExactCurrentTarget,
    PrivateTargetRecord,
    browser_selection_shape,
    resolve_exact_current_target,
)
from .native_projection import (
    NativeProjection,
    _private_projection_state,
    project_complete_tree,
    reset_projection_activity,
)
from .native_contracts import (
    NativeBoardSnapshot,
    NativeBrowserSelectionResult,
    NativeStopCommitResult,
    NativeStopPrepareResult,
    NativeStopStatusResult,
)
from .native_stop import NativeStop, StopClient
from .native_turns import project_exact_turn_list


SAFE_ERROR = "native_observation_unavailable"
DISCLOSURE = (
    "Polling may miss intermediate transitions; trail entries are observed endpoint "
    "differences, not native events."
)
DEFAULT_MAXIMUM_OBSERVATION_AGE_SECONDS = 5.0
_T = TypeVar("_T")


class NativeClient(Protocol):
    def thread_read(self, thread_id: str, *, include_turns: bool = True) -> Mapping[str, Any]: ...
    def thread_list(self, params: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def stop_request(self, method: str, params: Mapping[str, Any], *,
        max_response_bytes: int = 256 * 1024, timeout_seconds: float = 3.0,
        _close_after: bool = True,
    ) -> tuple[str, Mapping[str, Any] | None]: ...
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


class NativeBoard:
    """Poll complete native-tree passes and retain only a safe browser projection."""

    def __init__(
        self,
        client_factory: Callable[[], NativeClient],
        root_thread_id: str,
        *,
        poll_interval_seconds: float = 1.0,
        trail_limit: int = 50,
        maximum_observation_age_seconds: float = DEFAULT_MAXIMUM_OBSERVATION_AGE_SECONDS,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not root_thread_id:
            raise ValueError("an exact native root thread id is required")
        if poll_interval_seconds <= 0 or trail_limit <= 0:
            raise ValueError("poll interval and trail limit must be positive")
        if (
            isinstance(maximum_observation_age_seconds, bool)
            or not isinstance(maximum_observation_age_seconds, (int, float))
            or not math.isfinite(float(maximum_observation_age_seconds))
            or maximum_observation_age_seconds < 0
        ):
            raise ValueError("maximum observation age must be a finite nonnegative number")
        self._client_factory = client_factory
        self._root_thread_id = root_thread_id
        self._poll_interval = poll_interval_seconds
        self._trail_limit = trail_limit
        self._maximum_observation_age = float(maximum_observation_age_seconds)
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._poll_lock = threading.Lock()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._observation_run_ref = secrets.token_urlsafe(24)
        self._projection: NativeProjection | None = None
        self._completed_at: float | None = None
        self._connected = False
        self._error_code: str | None = None
        self._target_identities: dict[str, ExactCurrentTarget] = {}
        self._native_ids_by_target: dict[ExactCurrentTarget, str] = {}
        self._target_records: list[PrivateTargetRecord] = []
        self._turn_observations: dict[str, tuple[str, float]] = {}
        self._turn_probe_offset = 0
        self._stopper = NativeStop(cast(Callable[[], StopClient], client_factory), self._resolve_present)

    def start(self) -> None:
        self.poll_once()
        self._thread = threading.Thread(target=self._run, name="switchstand-native-board", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        with self._lock:
            self._closed = True
            self._connected = False

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval):
            self.poll_once()

    @staticmethod
    def _previously_issued(agent_ref: Any, issuance_watermark: int) -> bool:
        """Recognize one canonical run-local ref without parsing hostile integers."""
        if not isinstance(agent_ref, str) or not agent_ref.startswith("agent-"):
            return False
        suffix = agent_ref.removeprefix("agent-")
        maximum_digits = len(str(max(1, issuance_watermark - 1)))
        if (
            not suffix
            or len(suffix) > maximum_digits
            or suffix[0] == "0"
            or any(character < "0" or character > "9" for character in suffix)
        ):
            return False
        return int(suffix) < issuance_watermark

    def _selection_observation(self, agent_ref: Any = None) -> dict[str, Any]:
        if self._projection is None:
            labels: Mapping[str, str] = {}
            issuance_watermark = 1
            endpoints: Mapping[str, Mapping[str, Any]] = {}
        else:
            labels, issuance_watermark, _, endpoints, _ = _private_projection_state(
                self._projection
            )
        present = set(endpoints)
        records = [
            {"agentRef": ref, "present": ref in present}
            for ref in labels.values()
        ]
        if (
            self._previously_issued(agent_ref, issuance_watermark)
            and agent_ref not in present
        ):
            records.append({"agentRef": agent_ref, "present": False})
        return {
            "observationRunRef": self._observation_run_ref,
            "connected": self._connected and not self._closed,
            "latestCompletePassCompletedAt": self._completed_at,
            "agentRecords": records,
        }

    def poll_once(self) -> None:
        with self._poll_lock:
            self._poll_once_serialized()

    def _poll_once_serialized(self) -> None:
        with self._lock:
            if self._closed:
                return
        client: NativeClient | None = None
        try:
            client = self._client_factory()
            read_client = cast(AgentTreeClient, _StateDbOnlyClient(client))
            tree = AgentTreeAdapter(read_client).observe_tree(self._root_thread_id)
            completed_wall = self._wall_clock()
            completed_mono = self._monotonic()
            threads = list(tree["threads"])
            with self._lock:
                prior = self._projection
            projection = project_complete_tree(
                threads,
                prior_projection=prior,
                completed_at=completed_wall,
                completed_monotonic=completed_mono,
                trail_limit=self._trail_limit,
            )
            _, _, _, _, projected_targets = _private_projection_state(projection)
            probe: tuple[str, str, float] | None = None
            if projected_targets:
                index = self._turn_probe_offset % len(projected_targets)
                agent_ref, native_id = projected_targets[index]
                self._turn_probe_offset += 1
                try:
                    classification, response = client.stop_request(
                        "thread/turns/list",
                        {"threadId": native_id, "limit": 1, "sortDirection": "desc",
                            "itemsView": "notLoaded"},
                    )
                except Exception:
                    classification, response = "unavailable", None
                if classification == "ok" and response is not None:
                    if "target" not in response:
                        rebound = dict(response)
                        rebound["target"] = native_id
                        exact_turn = project_exact_turn_list(rebound, native_id)
                        if exact_turn is not None:
                            probe = (agent_ref, exact_turn.status, completed_wall)
            with self._lock:
                if self._closed:
                    return
                target_records = []
                target_identities: dict[str, ExactCurrentTarget] = {}
                native_ids_by_target: dict[ExactCurrentTarget, str] = {}
                _, _, _, _, targets = _private_projection_state(projection)
                for agent_ref, native_id in targets:
                    target = self._target_identities.get(native_id)
                    if target is None:
                        target = ExactCurrentTarget()
                    target_identities[native_id] = target
                    native_ids_by_target[target] = native_id
                    target_records.append(PrivateTargetRecord(agent_ref, target))
                self._projection = projection
                self._target_identities = target_identities
                self._native_ids_by_target = native_ids_by_target
                self._target_records = target_records
                present_refs = {record.agent_ref for record in target_records}
                self._turn_observations = {
                    ref: value for ref, value in self._turn_observations.items()
                    if ref in present_refs
                }
                if projected_targets:
                    queried_ref = projected_targets[(self._turn_probe_offset - 1) % len(projected_targets)][0]
                    self._turn_observations.pop(queried_ref, None)
                if probe is not None:
                    self._turn_observations[probe[0]] = (probe[1], probe[2])
                self._completed_at = completed_wall
                self._connected = True
                self._error_code = None
        except Exception:
            with self._lock:
                self._connected = False
                self._error_code = SAFE_ERROR
                self._turn_observations.clear()
                if self._projection is not None:
                    self._projection = reset_projection_activity(self._projection)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def snapshot(self) -> NativeBoardSnapshot:
        with self._lock:
            completed_at = self._completed_at
            now = self._wall_clock()
            agents = [] if self._projection is None else deepcopy(self._projection.agents)
            for agent in agents:
                agent["updatedAgeSeconds"] = round(max(0.0, now - agent["updatedAt"]), 3)
                observed = self._turn_observations.get(agent["agentRef"])
                agent["turnStatus"] = (
                    observed[0] if observed is not None
                    and now >= observed[1]
                    and now - observed[1] <= self._maximum_observation_age
                    else "unknown"
                )
            return cast(NativeBoardSnapshot, {
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
                "trail": [] if self._projection is None else deepcopy(self._projection.trail),
                "trailLimit": self._trail_limit,
                "disclosure": DISCLOSURE,
            })

    def resolve_current_target(
        self,
        selection: Any,
        *,
        now: Any,
        maximum_observation_age_seconds: Any,
    ) -> dict[str, Any] | ExactCurrentTarget:
        """Resolve the exact current private target for the #19 action lane."""
        with self._lock:
            return resolve_exact_current_target(
                selection,
                self._selection_observation(
                    selection.get("agentRef") if isinstance(selection, Mapping) else None
                ),
                tuple(self._target_records),
                now=now,
                maximum_observation_age_seconds=maximum_observation_age_seconds,
            )

    def _with_current_native_target(
        self,
        target: object,
        operation: Callable[[str], _T],
    ) -> _T | None:
        """Bind *target* from one locked snapshot, then run one private operation."""
        with self._lock:
            completed_at = self._completed_at
            now = self._wall_clock()
            fresh = (
                completed_at is not None
                and math.isfinite(completed_at)
                and completed_at <= now
                and now - completed_at <= self._maximum_observation_age
            )
            if (
                self._closed
                or not self._connected
                or not fresh
                or type(target) is not ExactCurrentTarget
            ):
                return None
            exact_target = cast(ExactCurrentTarget, target)
            matches = [
                record for record in self._target_records
                if record.target == exact_target
            ]
            native_id = self._native_ids_by_target.get(exact_target)
            if len(matches) != 1 or type(native_id) is not str or not native_id:
                return None
        return operation(native_id)

    def browser_selection(
        self,
        agent_ref: Any,
        *,
        now: Any,
        maximum_observation_age_seconds: Any,
    ) -> NativeBrowserSelectionResult:
        """Produce the minimal safe #17 selection pair/snapshot seam."""
        with self._lock:
            selection = {
                "observationRunRef": self._observation_run_ref,
                "agentRef": agent_ref,
            }
            return cast(NativeBrowserSelectionResult, browser_selection_shape(
                selection,
                self._selection_observation(agent_ref),
                now=now,
                maximum_observation_age_seconds=maximum_observation_age_seconds,
            ))

    def _resolve_present(self, agent_ref: str) -> str | None:
        with self._lock:
            selection = {
                "observationRunRef": self._observation_run_ref,
                "agentRef": agent_ref,
            }
            target = resolve_exact_current_target(
                selection,
                self._selection_observation(agent_ref),
                tuple(self._target_records),
                now=self._wall_clock(),
                maximum_observation_age_seconds=self._maximum_observation_age,
            )
            if not isinstance(target, ExactCurrentTarget):
                return None
            return self._native_ids_by_target.get(target)

    def prepare_stop(self, agent_ref: Any) -> NativeStopPrepareResult:
        return self._stopper.prepare(agent_ref)

    def commit_stop(self, confirmation_ref: Any) -> NativeStopCommitResult:
        return self._stopper.commit(confirmation_ref)

    def stop_status(self, operation_ref: Any) -> NativeStopStatusResult:
        return self._stopper.status(operation_ref)
