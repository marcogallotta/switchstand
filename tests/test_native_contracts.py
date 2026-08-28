from __future__ import annotations

import json
import unittest

from switchstand.native_contracts import (
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


class DeterministicBoard:
    def snapshot(self) -> NativeBoardSnapshot:
        return {
            "mode": "native",
            "observation": {
                "connected": True,
                "available": True,
                "historical": False,
                "errorCode": None,
                "completedAt": 12.5,
                "passAgeSeconds": 0.25,
                "kind": "completed_multi_request_pass",
            },
            "agents": [{
                "agentRef": "agent-1",
                "label": "Agent 1",
                "parentRef": None,
                "depth": 0,
                "sourceKind": "cli",
                "sourceDetail": None,
                "createdAt": 10.0,
                "updatedAt": 12.0,
                "status": "idle",
                "turnStatus": "none",
                "activeFlags": [],
                "activeObservedSeconds": 0.0,
                "updatedAgeSeconds": 0.75,
            }],
            "trail": [{
                "observedAt": 12.5,
                "agentRef": "agent-1",
                "changes": {"status": {"from": "active", "to": "idle"}},
            }],
            "trailLimit": 50,
            "disclosure": "Observed endpoint differences only.",
        }

    def browser_selection(
        self,
        agent_ref: object,
        *,
        now: object,
        maximum_observation_age_seconds: object,
    ) -> NativeBrowserSelectionResult:
        del agent_ref, now, maximum_observation_age_seconds
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


class DeterministicInput:
    def send(self, request: object) -> NativeInputResult:
        del request
        return {"code": "input_sent", "outcome": "sent", "mode": "start"}


class DeterministicStop:
    def prepare_stop(self, agent_ref: object) -> NativeStopPrepareResult:
        del agent_ref
        return {"code": "prepared", "agentRef": "agent-1", "confirmationRef": "confirm-1"}

    def commit_stop(self, confirmation_ref: object) -> NativeStopCommitResult:
        del confirmation_ref
        return {"code": "stop_result", "operationRef": "operation-1", "outcome": "requested"}

    def stop_status(self, operation_ref: object) -> NativeStopStatusResult:
        del operation_ref
        return {"code": "stop_result", "operationRef": "operation-1", "outcome": "confirmed"}


BOARD_PORT: NativeBoardSnapshotPort = DeterministicBoard()
SELECTION_PORT: NativeBrowserSelectionPort = DeterministicBoard()
INPUT_PORT: NativeInputPort = DeterministicInput()
STOP_PORT: NativeStopPort = DeterministicStop()


class NativeContractsTests(unittest.TestCase):
    def test_closed_fake_ports_preserve_golden_runtime_values(self):
        values = [
            BOARD_PORT.snapshot(),
            SELECTION_PORT.browser_selection(
                "agent-1", now=12.75, maximum_observation_age_seconds=5.0
            ),
            INPUT_PORT.send({}),
            STOP_PORT.prepare_stop("agent-1"),
            STOP_PORT.commit_stop("confirm-1"),
            STOP_PORT.stop_status("operation-1"),
        ]

        encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))

        self.assertEqual(json.loads(encoded), values)
        self.assertNotIn("PRIVATE-TARGET", encoded)
        self.assertEqual(values[2], {"code": "input_sent", "outcome": "sent", "mode": "start"})


if __name__ == "__main__":
    unittest.main()
