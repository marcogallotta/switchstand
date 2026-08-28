"""Narrow in-process facade over the frozen native workbench ports."""
from __future__ import annotations

import math
import time
from collections.abc import Callable

from .native_contracts import (
    NativeBoardSnapshotPort,
    NativeBrowserSelectionPort,
    NativeBrowserSelectionResult,
    NativeEvidenceRequest,
    NativeEvidenceResult,
    NativeInputPort,
    NativeInputResult,
    NativeStopCommitResult,
    NativeStopPort,
    NativeStopPrepareResult,
    NativeStopStatusResult,
    NativeWorkbenchSnapshot,
)
from .native_evidence import NativeEvidence, unavailable_evidence_summary


class NativeWorkbench:
    """Delegate one native request to one exact frozen domain port."""

    def __init__(
        self,
        board: NativeBoardSnapshotPort,
        selection: NativeBrowserSelectionPort,
        native_input: NativeInputPort,
        stop: NativeStopPort,
        *,
        maximum_observation_age_seconds: float,
        clock: Callable[[], float] = time.time,
        duration_clock: Callable[[], float] = time.perf_counter,
        evidence: NativeEvidence | None = None,
    ) -> None:
        if (
            isinstance(maximum_observation_age_seconds, bool)
            or not isinstance(maximum_observation_age_seconds, (int, float))
            or not math.isfinite(maximum_observation_age_seconds)
            or maximum_observation_age_seconds < 0
        ):
            raise ValueError("maximum observation age must be finite and non-negative")
        self._board = board
        self._selection = selection
        self._native_input = native_input
        self._stop = stop
        self._maximum_observation_age_seconds = float(maximum_observation_age_seconds)
        self._clock = clock
        self._duration_clock = duration_clock
        self._evidence = evidence or NativeEvidence()
        self._evidence_failed = False

    def _duration_start(self) -> float | None:
        try:
            value = float(self._duration_clock())
            return value if math.isfinite(value) else None
        except (TypeError, ValueError):
            return None

    def _duration_ms(self, started: float | None) -> float | None:
        if started is None:
            return None
        try:
            elapsed = (float(self._duration_clock()) - started) * 1000
            return elapsed if math.isfinite(elapsed) and elapsed >= 0 else None
        except (TypeError, ValueError):
            return None

    def _record(self, kind: object, outcome: object, *, started: float | None = None) -> None:
        if self._evidence_failed:
            return
        try:
            self._evidence.record(  # type: ignore[arg-type]
                kind,
                outcome,
                duration_ms=self._duration_ms(started),
            )
        except Exception:
            self._evidence_failed = True

    def workbench(self) -> NativeWorkbenchSnapshot:
        board = self._board.snapshot()
        if not self._evidence_failed:
            try:
                self._evidence.observe_board(board)
            except Exception:
                self._evidence_failed = True
        try:
            evidence = (
                unavailable_evidence_summary()
                if self._evidence_failed
                else self._evidence.snapshot()
            )
        except Exception:
            self._evidence_failed = True
            evidence = unavailable_evidence_summary()
        return {**board, "evidence": evidence}

    def resolve_selection(self, agent_ref: object) -> NativeBrowserSelectionResult:
        started = self._duration_start()
        try:
            result = self._selection.browser_selection(
                agent_ref,
                now=self._clock(),
                maximum_observation_age_seconds=self._maximum_observation_age_seconds,
            )
        except Exception:
            self._record("selection", "unavailable", started=started)
            raise
        snapshot = result["snapshot"]
        if "code" not in snapshot:
            outcome = "selected"
        else:
            outcome = {
                "INVALID_AGENT_REF": "invalid_agent_ref",
                "APP_SERVER_DISCONNECTED": "app_server_disconnected",
                "OBSERVATION_STALE": "observation_stale",
                "AGENT_NOT_PRESENT": "agent_not_present",
            }[snapshot["code"]]
        self._record("selection", outcome, started=started)
        return result

    def send_input(self, request: object) -> NativeInputResult:
        started = self._duration_start()
        try:
            result = self._native_input.send(request)
        except Exception:
            self._record("input", "unavailable", started=started)
            raise
        outcome = (
            f"sent_{result['mode']}"
            if result["code"] == "input_sent"
            else "not_sent"
        )
        self._record("input", outcome, started=started)
        return result

    def prepare_stop(self, agent_ref: object) -> NativeStopPrepareResult:
        started = self._duration_start()
        try:
            result = self._stop.prepare_stop(agent_ref)
        except Exception:
            self._record("stop_prepare", "unavailable", started=started)
            raise
        outcome = {
            "prepared": "prepared",
            "target_unavailable": "not_sent_target_unavailable",
            "stop_capacity": "not_sent_capacity",
        }[result["code"]]
        self._record("stop_prepare", outcome, started=started)
        return result

    def commit_stop(self, confirmation_ref: object) -> NativeStopCommitResult:
        started = self._duration_start()
        try:
            result = self._stop.commit_stop(confirmation_ref)
        except Exception:
            self._record("stop_commit", "unavailable", started=started)
            raise
        outcome = (
            "not_sent_confirmation_unavailable"
            if result["code"] == "confirmation_unavailable"
            else result["outcome"]
        )
        self._record("stop_commit", outcome, started=started)
        return result

    def stop_status(self, operation_ref: object) -> NativeStopStatusResult:
        started = self._duration_start()
        try:
            result = self._stop.stop_status(operation_ref)
        except Exception:
            self._record("stop_status", "unavailable", started=started)
            raise
        outcome = (
            "not_sent_operation_unavailable"
            if result["code"] == "operation_unavailable"
            else result["outcome"]
        )
        self._record("stop_status", outcome, started=started)
        return result

    def record_browser_evidence(self, request: NativeEvidenceRequest) -> NativeEvidenceResult:
        if self._evidence_failed:
            return {"code": "evidence_unavailable", "outcome": "not_recorded"}
        try:
            self._evidence.record_browser_event(request["event"])
        except Exception:
            self._evidence_failed = True
            return {"code": "evidence_unavailable", "outcome": "not_recorded"}
        return {"code": "evidence_recorded", "outcome": "recorded"}
