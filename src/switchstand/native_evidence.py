"""Bounded, privacy-safe evidence for the native operator surface."""
from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable

from .native_contracts import (
    NativeBoardSnapshot,
    NativeEvidenceEvent,
    NativeEvidenceEventKind,
    NativeEvidenceOutcome,
    NativeEvidenceSummary,
    NativeStatusCounts,
    NativeTurnStatusCounts,
)

_MAX_COUNTER = 2_147_483_647
_MAX_DURATION_MS = 3_600_000
_MAX_AGE_SECONDS = 31_536_000.0
_ALLOWED: dict[NativeEvidenceEventKind, frozenset[NativeEvidenceOutcome]] = {
    "observation": frozenset({"connected", "disconnected"}),
    "selection": frozenset({
        "selected", "invalid_agent_ref", "app_server_disconnected",
        "observation_stale", "agent_not_present", "unavailable",
    }),
    "input": frozenset({"sent_start", "sent_steer", "not_sent", "unavailable"}),
    "stop_prepare": frozenset({
        "prepared", "not_sent_target_unavailable", "not_sent_capacity", "unavailable",
    }),
    "stop_commit": frozenset({
        "requested", "rejected", "unknown", "not_sent_confirmation_unavailable", "unavailable",
    }),
    "stop_status": frozenset({
        "requested", "rejected", "confirmed", "not_confirmed", "unknown",
        "not_sent_operation_unavailable", "unavailable",
    }),
    "focus_invariant": frozenset({"failed"}),
    "refresh": frozenset({"coalesced"}),
    "stop_cancel": frozenset({"not_sent"}),
}


def _increment(value: int) -> int:
    return min(_MAX_COUNTER, value + 1)


def _bounded_number(value: object, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return min(number, maximum)


class NativeEvidence:
    """Keep a fixed-schema, bounded, process-local evidence window."""

    def __init__(
        self,
        *,
        capacity: int = 50,
        duplicate_window_seconds: float = 1.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 500:
            raise ValueError("capacity must be an integer from 1 to 500")
        if (
            isinstance(duplicate_window_seconds, bool)
            or not isinstance(duplicate_window_seconds, (int, float))
            or not math.isfinite(duplicate_window_seconds)
            or duplicate_window_seconds < 0
            or duplicate_window_seconds > 60
        ):
            raise ValueError("duplicate window must be finite and from 0 to 60 seconds")
        self._capacity = capacity
        self._duplicate_window_seconds = float(duplicate_window_seconds)
        self._clock = clock
        self._events: deque[NativeEvidenceEvent] = deque()
        self._last_event_key: tuple[str, str] | None = None
        self._last_event_at: float | None = None
        self._dropped_count = 0
        self._duplicate_count = 0
        self._refresh_count = 0
        self._coalesced_refresh_count = 0
        self._observation_connected: bool | None = None
        self._pass_age_seconds: float | None = None
        self._agent_count = 0
        self._status_counts: NativeStatusCounts = {
            "active": 0, "idle": 0, "systemError": 0, "notLoaded": 0,
        }
        self._turn_status_counts: NativeTurnStatusCounts = {
            "unknown": 0, "none": 0, "inProgress": 0,
            "completed": 0, "failed": 0, "interrupted": 0,
        }
        self._last_observed_activity_age_seconds: float | None = None

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("clock must return a number")
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise ValueError("clock must return a finite non-negative number")
        return result

    def record(
        self,
        kind: NativeEvidenceEventKind,
        outcome: NativeEvidenceOutcome,
        *,
        duration_ms: object = None,
        pass_age_seconds: object = None,
    ) -> bool:
        if kind not in _ALLOWED or outcome not in _ALLOWED[kind]:
            raise ValueError("event kind/outcome is not allowed")
        duration = _bounded_number(duration_ms, float(_MAX_DURATION_MS))
        if duration_ms is not None and duration is None:
            raise ValueError("duration must be finite and non-negative")
        age = _bounded_number(pass_age_seconds, _MAX_AGE_SECONDS)
        if pass_age_seconds is not None and age is None:
            raise ValueError("pass age must be finite and non-negative")
        now = self._now()
        key = (kind, outcome)
        if (
            kind in {"observation", "focus_invariant", "refresh", "stop_cancel"}
            and self._last_event_key == key
            and self._last_event_at is not None
            and 0 <= now - self._last_event_at <= self._duplicate_window_seconds
        ):
            self._duplicate_count = _increment(self._duplicate_count)
            if kind == "refresh":
                self._coalesced_refresh_count = _increment(self._coalesced_refresh_count)
            return False
        event: NativeEvidenceEvent = {
            "observedAt": now,
            "kind": kind,
            "outcome": outcome,
            "durationMs": None if duration is None else int(round(duration)),
            "passAgeSeconds": age,
        }
        if len(self._events) >= self._capacity:
            self._events.popleft()
            self._dropped_count = _increment(self._dropped_count)
        self._events.append(event)
        self._last_event_key = key
        self._last_event_at = now
        if kind == "refresh":
            self._coalesced_refresh_count = _increment(self._coalesced_refresh_count)
        return True

    def observe_board(self, board: NativeBoardSnapshot) -> None:
        self._refresh_count = _increment(self._refresh_count)
        observation = board["observation"]
        connected = observation["connected"]
        pass_age = _bounded_number(observation["passAgeSeconds"], _MAX_AGE_SECONDS)
        if connected != self._observation_connected:
            self.record(
                "observation",
                "connected" if connected else "disconnected",
                pass_age_seconds=pass_age,
            )
        self._observation_connected = connected
        self._pass_age_seconds = pass_age

        statuses: NativeStatusCounts = {
            "active": 0, "idle": 0, "systemError": 0, "notLoaded": 0,
        }
        turn_statuses: NativeTurnStatusCounts = {
            "unknown": 0, "none": 0, "inProgress": 0,
            "completed": 0, "failed": 0, "interrupted": 0,
        }
        ages: list[float] = []
        for agent in board["agents"]:
            statuses[agent["status"]] += 1
            turn_statuses[agent["turnStatus"]] += 1
            age = _bounded_number(agent["updatedAgeSeconds"], _MAX_AGE_SECONDS)
            if age is not None:
                ages.append(age)
        self._agent_count = min(len(board["agents"]), _MAX_COUNTER)
        self._status_counts = statuses
        self._turn_status_counts = turn_statuses
        self._last_observed_activity_age_seconds = min(ages) if ages else None

    def record_browser_event(self, event: object) -> bool:
        mapping: dict[str, tuple[NativeEvidenceEventKind, NativeEvidenceOutcome]] = {
            "focus_preservation_failed": ("focus_invariant", "failed"),
            "refresh_coalesced": ("refresh", "coalesced"),
            "stop_cancelled": ("stop_cancel", "not_sent"),
        }
        if type(event) is not str or event not in mapping:
            raise ValueError("browser event is not allowed")
        return self.record(*mapping[event])

    def snapshot(self) -> NativeEvidenceSummary:
        return {
            "available": True,
            "storage": "bounded_process_memory",
            "capacity": self._capacity,
            "retainedCount": len(self._events),
            "droppedCount": self._dropped_count,
            "duplicateCount": self._duplicate_count,
            "refreshCount": self._refresh_count,
            "coalescedRefreshCount": self._coalesced_refresh_count,
            "observationConnected": self._observation_connected,
            "passAgeSeconds": self._pass_age_seconds,
            "agentCount": self._agent_count,
            "statusCounts": self._status_counts.copy(),
            "turnStatusCounts": self._turn_status_counts.copy(),
            "lastObservedActivityAgeSeconds": self._last_observed_activity_age_seconds,
            "recentEvents": [event.copy() for event in self._events],
            "disclosure": (
                "Bounded process-local objective evidence only; restart clears it. "
                "No text, identifiers, paths, raw errors, screenshots, or inferred intent."
            ),
        }


def unavailable_evidence_summary() -> NativeEvidenceSummary:
    """Return the fixed fail-closed summary used after recorder failure."""
    return {
        "available": False,
        "storage": "bounded_process_memory",
        "capacity": 0,
        "retainedCount": 0,
        "droppedCount": 0,
        "duplicateCount": 0,
        "refreshCount": 0,
        "coalescedRefreshCount": 0,
        "observationConnected": None,
        "passAgeSeconds": None,
        "agentCount": 0,
        "statusCounts": {"active": 0, "idle": 0, "systemError": 0, "notLoaded": 0},
        "turnStatusCounts": {
            "unknown": 0, "none": 0, "inProgress": 0,
            "completed": 0, "failed": 0, "interrupted": 0,
        },
        "lastObservedActivityAgeSeconds": None,
        "recentEvents": [],
        "disclosure": "Evidence recording is unavailable; no positive evidence claim is made.",
    }
