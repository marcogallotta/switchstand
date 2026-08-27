from __future__ import annotations

import json
import unittest

from switchstand.current_target import (
    ExactCurrentTarget,
    PrivateTargetRecord,
    browser_selection_shape,
    resolve_exact_current_target,
)


def observation(
    *,
    run_ref: str = "run-current",
    connected: bool = True,
    completed_at: float = 100.0,
    records: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "observationRunRef": run_ref,
        "connected": connected,
        "latestCompletePassCompletedAt": completed_at,
        "agentRecords": (
            [{"agentRef": "agent-1", "present": True}] if records is None else records
        ),
        "nativeUpdatedAt": 10_000_000.0,
        "activityAt": 10_000_000.0,
        "trail": [{"observedAt": 10_000_000.0}],
        "turnTiming": 10_000_000.0,
        "transcriptTiming": 10_000_000.0,
    }


def selection(run_ref: str = "run-current", agent_ref: str = "agent-1") -> dict[str, str]:
    return {"observationRunRef": run_ref, "agentRef": agent_ref}


def error_code(value: dict[str, object] | ExactCurrentTarget) -> object:
    if not isinstance(value, dict):
        raise AssertionError("expected a frozen safe error")
    return value["code"]


class CurrentTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = ExactCurrentTarget()
        self.targets = [PrivateTargetRecord("agent-1", self.target)]

    def resolve(
        self,
        selected: object | None = None,
        current: object | None = None,
        targets: object | None = None,
        *,
        now: object = 105.0,
        maximum: object = 5.0,
    ):
        return resolve_exact_current_target(
            selection() if selected is None else selected,
            observation() if current is None else current,
            self.targets if targets is None else targets,
            now=now,
            maximum_observation_age_seconds=maximum,
        )

    def test_exact_pair_requires_unique_public_record_and_private_target(self):
        self.assertIs(self.resolve(), self.target)
        invalid = {"code": "INVALID_AGENT_REF", "message": "Selected agent is unavailable."}
        cases = [
            (selection(agent_ref="missing"), observation(), self.targets),
            (
                selection(),
                observation(
                    records=[
                        {"agentRef": "agent-1", "present": True},
                        {"agentRef": "agent-1", "present": True},
                    ]
                ),
                self.targets,
            ),
            (selection(), observation(), []),
            (selection(), observation(), self.targets * 2),
            (selection("run-before"), observation(), self.targets),
        ]
        for selected, current, targets in cases:
            with self.subTest(selected=selected, target_count=len(targets)):
                self.assertEqual(self.resolve(selected, current, targets), invalid)

    def test_connection_freshness_and_presence_fail_with_frozen_safe_results(self):
        self.assertEqual(
            self.resolve(current=observation(connected=False)),
            {"code": "APP_SERVER_DISCONNECTED", "message": "Agent connection is unavailable."},
        )
        self.assertEqual(
            self.resolve(now=105.001),
            {"code": "OBSERVATION_STALE", "message": "Selected agent observation is stale."},
        )
        self.assertEqual(
            self.resolve(current=observation(records=[{"agentRef": "agent-1", "present": False}])),
            {"code": "AGENT_NOT_PRESENT", "message": "Selected agent is no longer present."},
        )

    def test_freshness_uses_only_latest_complete_pass_and_includes_threshold(self):
        self.assertIs(self.resolve(now=104.999), self.target)
        self.assertIs(self.resolve(now=105.0), self.target)
        stale = self.resolve(now=105.001)
        self.assertEqual(error_code(stale), "OBSERVATION_STALE")
        changed_unrelated_timing = observation()
        for field in (
            "nativeUpdatedAt",
            "activityAt",
            "trail",
            "turnTiming",
            "transcriptTiming",
        ):
            changed_unrelated_timing[field] = 999_999_999.0
        self.assertEqual(
            error_code(self.resolve(current=changed_unrelated_timing, now=105.001)),
            "OBSERVATION_STALE",
        )

    def test_opaque_target_supports_fencing_without_disclosing_identity(self):
        same = self.resolve()
        changed = ExactCurrentTarget()
        self.assertEqual(same, self.target)
        self.assertNotEqual(changed, self.target)
        self.assertNotIn("raw-thread", repr(self.target))
        self.assertEqual(repr(self.target), "<ExactCurrentTarget opaque>")
        with self.assertRaises(TypeError):
            json.dumps(self.target)

    def test_browser_shape_is_closed_to_pair_and_frozen_snapshot(self):
        shape = browser_selection_shape(
            selection(),
            {**observation(), "rawThreadId": "raw-thread-secret"},
            now=105.0,
            maximum_observation_age_seconds=5.0,
        )
        self.assertEqual(set(shape), {"selection", "snapshot"})
        self.assertEqual(set(shape["selection"]), {"observationRunRef", "agentRef"})
        self.assertEqual(shape["snapshot"]["version"], "native-selection-v1")
        emitted = json.dumps(shape)
        self.assertNotIn("raw-thread-secret", emitted)
        self.assertNotIn("nativeUpdatedAt", emitted)

        invalid = browser_selection_shape(
            selection(agent_ref="RAW-THREAD-SENTINEL"),
            observation(),
            now=105.0,
            maximum_observation_age_seconds=5.0,
        )
        self.assertIsNone(invalid["selection"])
        self.assertEqual(invalid["snapshot"]["code"], "INVALID_AGENT_REF")
        self.assertNotIn("RAW-THREAD-SENTINEL", json.dumps(invalid))


if __name__ == "__main__":
    unittest.main()
