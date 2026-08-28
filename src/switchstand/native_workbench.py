"""Narrow in-process facade over the frozen native workbench ports."""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import TypeVar, cast

from .native_contracts import (
    NativeBoardSnapshotPort,
    NativeBrowserSelectionPort,
    NativeBrowserSelectionResult,
    NativeEvidenceEventKind,
    NativeEvidenceOutcome,
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


T = TypeVar("T")


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
        self._evidence = evidence if evidence is not None else NativeEvidence()
        self._evidence_failed = False

    def _duration(self, started: float) -> float | None:
        try:
            elapsed = (float(self._duration_clock()) - started) * 1000
            return elapsed if math.isfinite(elapsed) and elapsed >= 0 else None
        except Exception:
            return None

    def _record(
        self,
        kind: NativeEvidenceEventKind,
        outcome: NativeEvidenceOutcome,
        *,
        duration_ms: float | None = None,
    ) -> None:
        if self._evidence_failed:
            return
        try:
            self._evidence.record(kind, outcome, duration_ms=duration_ms)
        except Exception:
            self._evidence_failed = True

    def _observed_call(
        self,
        kind: NativeEvidenceEventKind,
        operation: Callable[[], T],
        outcome_for: Callable[[T], NativeEvidenceOutcome],
    ) -> T:
        try:
            started = float(self._duration_clock())
        except Exception:
            started = float("nan")
        try:
            result = operation()
        except Exception:
            self._record(kind, "unavailable", duration_ms=self._duration(started))
            raise
        self._record(kind, outcome_for(result), duration_ms=self._duration(started))
        return result

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

    @staticmethod
    def _selection_outcome(result: NativeBrowserSelectionResult) -> NativeEvidenceOutcome:
        snapshot = result["snapshot"]
        if "code" not in snapshot:
            return "selected"
        return cast(NativeEvidenceOutcome, {
            "INVALID_AGENT_REF": "invalid_agent_ref",
            "APP_SERVER_DISCONNECTED": "app_server_disconnected",
            "OBSERVATION_STALE": "observation_stale",
            "AGENT_NOT_PRESENT": "agent_not_present",
        }[snapshot["code"]])

    def resolve_selection(self, agent_ref: object) -> NativeBrowserSelectionResult:
        return self._observed_call(
            "selection",
            lambda: self._selection.browser_selection(
                agent_ref,
                now=self._clock(),
                maximum_observation_age_seconds=self._maximum_observation_age_seconds,
            ),
            self._selection_outcome,
        )

    @staticmethod
    def _input_outcome(result: NativeInputResult) -> NativeEvidenceOutcome:
        if result["code"] != "input_sent":
            return "not_sent"
        return "sent_start" if result["mode"] == "start" else "sent_steer"

    def send_input(self, request: object) -> NativeInputResult:
        return self._observed_call(
            "input",
            lambda: self._native_input.send(request),
            self._input_outcome,
        )

    @staticmethod
    def _stop_prepare_outcome(result: NativeStopPrepareResult) -> NativeEvidenceOutcome:
        return cast(NativeEvidenceOutcome, {
            "prepared": "prepared",
            "target_unavailable": "not_sent_target_unavailable",
            "stop_capacity": "not_sent_capacity",
        }[result["code"]])

    def prepare_stop(self, agent_ref: object) -> NativeStopPrepareResult:
        return self._observed_call(
            "stop_prepare",
            lambda: self._stop.prepare_stop(agent_ref),
            self._stop_prepare_outcome,
        )

    @staticmethod
    def _stop_result_outcome(
        result: NativeStopCommitResult | NativeStopStatusResult,
        unavailable_code: str,
        unavailable_outcome: NativeEvidenceOutcome,
    ) -> NativeEvidenceOutcome:
        if result["code"] == unavailable_code:
            return unavailable_outcome
        return cast(NativeEvidenceOutcome, result["outcome"])

    def commit_stop(self, confirmation_ref: object) -> NativeStopCommitResult:
        return self._observed_call(
            "stop_commit",
            lambda: self._stop.commit_stop(confirmation_ref),
            lambda result: self._stop_result_outcome(
                result, "confirmation_unavailable", "not_sent_confirmation_unavailable"
            ),
        )

    def stop_status(self, operation_ref: object) -> NativeStopStatusResult:
        return self._observed_call(
            "stop_status",
            lambda: self._stop.stop_status(operation_ref),
            lambda result: self._stop_result_outcome(
                result, "operation_unavailable", "not_sent_operation_unavailable"
            ),
        )

    def record_browser_evidence(self, request: NativeEvidenceRequest) -> NativeEvidenceResult:
        if self._evidence_failed:
            return {"code": "evidence_unavailable", "outcome": "not_recorded"}
        try:
            self._evidence.record_browser_event(request["event"])
        except Exception:
            self._evidence_failed = True
            return {"code": "evidence_unavailable", "outcome": "not_recorded"}
        return {"code": "evidence_recorded", "outcome": "recorded"}
