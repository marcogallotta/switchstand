from __future__ import annotations

import unittest

from switchstand.native_contracts import (
    NativeBoardSnapshot,
    NativeBrowserSelectionResult,
    NativeInputResult,
    NativeStopCommitResult,
    NativeStopPrepareResult,
    NativeStopStatusResult,
)
from switchstand.native_workbench import NativeWorkbench


def board_snapshot() -> NativeBoardSnapshot:
    return {
        "mode": "native",
        "observation": {
            "connected": True,
            "available": True,
            "historical": False,
            "errorCode": None,
            "completedAt": 10.0,
            "passAgeSeconds": 0.0,
            "kind": "completed_multi_request_pass",
        },
        "agents": [],
        "trail": [],
        "trailLimit": 50,
        "disclosure": "Observed differences only.",
    }


class Ports:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def snapshot(self) -> NativeBoardSnapshot:
        self.calls.append(("snapshot",))
        return board_snapshot()

    def browser_selection(
        self,
        agent_ref: object,
        *,
        now: object,
        maximum_observation_age_seconds: object,
    ) -> NativeBrowserSelectionResult:
        self.calls.append(("selection", agent_ref, now, maximum_observation_age_seconds))
        return {
            "selection": {"observationRunRef": "run-1", "agentRef": "agent-1"},
            "snapshot": {
                "version": "native-selection-v1",
                "observationRunRef": "run-1",
                "agentRef": "agent-1",
                "connected": True,
                "present": True,
            },
        }

    def send(self, request: object) -> NativeInputResult:
        self.calls.append(("input", request))
        return {"code": "input_sent", "outcome": "sent", "mode": "steer"}

    def prepare_stop(self, agent_ref: object) -> NativeStopPrepareResult:
        self.calls.append(("prepare", agent_ref))
        return {"code": "prepared", "agentRef": "agent-1", "confirmationRef": "confirm-1"}

    def commit_stop(self, confirmation_ref: object) -> NativeStopCommitResult:
        self.calls.append(("commit", confirmation_ref))
        return {"code": "stop_result", "operationRef": "operation-1", "outcome": "requested"}

    def stop_status(self, operation_ref: object) -> NativeStopStatusResult:
        self.calls.append(("status", operation_ref))
        return {"code": "stop_result", "operationRef": "operation-1", "outcome": "confirmed"}


class NativeWorkbenchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ports = Ports()
        self.workbench = NativeWorkbench(
            self.ports,
            self.ports,
            self.ports,
            self.ports,
            maximum_observation_age_seconds=4.5,
            clock=lambda: 12.25,
        )

    def test_each_facade_call_delegates_exactly_once_without_shape_drift(self):
        input_request = {
            "version": "native-input-v1",
            "observationRunRef": "run-1",
            "agentRef": "agent-1",
            "text": "exact text",
        }
        results = (
            self.workbench.workbench(),
            self.workbench.resolve_selection("agent-1"),
            self.workbench.send_input(input_request),
            self.workbench.prepare_stop("agent-1"),
            self.workbench.commit_stop("confirm-1"),
            self.workbench.stop_status("operation-1"),
        )

        self.assertEqual(results[0], board_snapshot())
        selection = results[1]["selection"]
        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual(selection["agentRef"], "agent-1")
        self.assertEqual(results[2], {"code": "input_sent", "outcome": "sent", "mode": "steer"})
        self.assertEqual([call[0] for call in self.ports.calls], [
            "snapshot", "selection", "input", "prepare", "commit", "status"
        ])
        self.assertEqual(self.ports.calls[1], ("selection", "agent-1", 12.25, 4.5))
        self.assertIs(self.ports.calls[2][1], input_request)

    def test_constructor_rejects_only_invalid_freshness_configuration(self):
        for value in (True, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                NativeWorkbench(
                    self.ports,
                    self.ports,
                    self.ports,
                    self.ports,
                    maximum_observation_age_seconds=value,
                )


if __name__ == "__main__":
    unittest.main()
