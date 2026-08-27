"""Narrow in-process facade over the frozen native workbench ports."""
from __future__ import annotations

import math
import time
from collections.abc import Callable

from .native_contracts import (
    NativeBoardSnapshot,
    NativeBoardSnapshotPort,
    NativeBrowserSelectionPort,
    NativeBrowserSelectionResult,
    NativeInputPort,
    NativeInputResult,
    NativeStopCommitResult,
    NativeStopPort,
    NativeStopPrepareResult,
    NativeStopStatusResult,
)


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

    def workbench(self) -> NativeBoardSnapshot:
        return self._board.snapshot()

    def resolve_selection(self, agent_ref: object) -> NativeBrowserSelectionResult:
        return self._selection.browser_selection(
            agent_ref,
            now=self._clock(),
            maximum_observation_age_seconds=self._maximum_observation_age_seconds,
        )

    def send_input(self, request: object) -> NativeInputResult:
        return self._native_input.send(request)

    def prepare_stop(self, agent_ref: object) -> NativeStopPrepareResult:
        return self._stop.prepare_stop(agent_ref)

    def commit_stop(self, confirmation_ref: object) -> NativeStopCommitResult:
        return self._stop.commit_stop(confirmation_ref)

    def stop_status(self, operation_ref: object) -> NativeStopStatusResult:
        return self._stop.stop_status(operation_ref)
