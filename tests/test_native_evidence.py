from __future__ import annotations

import unittest

from switchstand.native_contracts import NativeAgent, NativeBoardSnapshot
from switchstand.native_evidence import NativeEvidence, unavailable_evidence_summary


def board(
    *,
    connected: bool = True,
    pass_age: float | None = 0.25,
    agents: list[NativeAgent] | None = None,
) -> NativeBoardSnapshot:
    observation = {"connected": connected, "available": connected, "historical": not connected,
        "errorCode": None, "completedAt": 10.0, "passAgeSeconds": pass_age,
        "kind": "completed_multi_request_pass"}
    return {"mode": "native", "observation": observation, "agents": agents or [],
        "trail": [], "trailLimit": 50, "disclosure": "Observed differences only."}


class NativeEvidenceTests(unittest.TestCase):
    def test_summary_contains_only_bounded_categories_and_no_identifiers_or_text(self):
        moments = iter((10.0, 12.0, 13.0))
        evidence = NativeEvidence(capacity=2, clock=lambda: next(moments))
        evidence.observe_board(board(agents=[{
            "agentRef": "PRIVATE-THREAD", "label": "PRIVATE LABEL", "parentRef": None,
            "depth": 0, "sourceKind": "cli", "sourceDetail": "PRIVATE SOURCE",
            "createdAt": 1.0, "updatedAt": 2.0, "status": "active",
            "turnStatus": "inProgress", "activeFlags": ["PRIVATE FLAG"],
            "activeObservedSeconds": 3.0, "updatedAgeSeconds": 4.0,
        }]))
        evidence.record("input", "sent_start", duration_ms=15)
        evidence.record("stop_commit", "requested", duration_ms=20)

        summary = evidence.snapshot()
        self.assertEqual(summary["retainedCount"], 2)
        self.assertEqual(summary["droppedCount"], 1)
        self.assertEqual(summary["agentCount"], 1)
        self.assertEqual(summary["statusCounts"]["active"], 1)
        self.assertEqual(summary["turnStatusCounts"]["inProgress"], 1)
        self.assertEqual(summary["lastObservedActivityAgeSeconds"], 4.0)
        serialized = repr(summary)
        for forbidden in ("PRIVATE-THREAD", "PRIVATE LABEL", "PRIVATE SOURCE", "PRIVATE FLAG"):
            self.assertNotIn(forbidden, serialized)

    def test_invalid_values_and_kind_outcome_pairs_are_rejected(self):
        evidence = NativeEvidence(clock=lambda: 1.0)
        self.assertTrue(evidence.record("stop_commit", "not_sent"))
        self.assertTrue(evidence.record("stop_status", "not_sent"))
        for call in (
            lambda: evidence.record("input", "confirmed"),
            lambda: evidence.record("input", "sent_start", duration_ms=-1),
            lambda: evidence.record_browser_event("arbitrary"),
        ):
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()

    def test_duplicate_events_coalesce_and_counters_saturate_bounded_storage(self):
        moments = iter((1.0, 1.25, 2.5))
        evidence = NativeEvidence(capacity=1, duplicate_window_seconds=1.0, clock=lambda: next(moments))
        self.assertTrue(evidence.record_browser_event("focus_preservation_failed"))
        self.assertFalse(evidence.record_browser_event("focus_preservation_failed"))
        self.assertTrue(evidence.record_browser_event("focus_preservation_failed"))
        summary = evidence.snapshot()
        self.assertEqual(summary["retainedCount"], 1)
        self.assertEqual(summary["duplicateCount"], 1)
        self.assertEqual(summary["droppedCount"], 1)

    def test_observation_transition_refresh_and_coalescing_are_objective(self):
        moments = iter((1.0, 3.0, 5.0))
        evidence = NativeEvidence(clock=lambda: next(moments))
        evidence.observe_board(board(connected=True))
        evidence.observe_board(board(connected=True))
        evidence.observe_board(board(connected=False, pass_age=None))
        evidence.record_browser_event("refresh_coalesced")
        summary = evidence.snapshot()
        self.assertEqual(summary["refreshCount"], 3)
        self.assertEqual(summary["coalescedRefreshCount"], 1)
        self.assertEqual(
            [(item["kind"], item["outcome"]) for item in summary["recentEvents"]],
            [("observation", "connected"), ("observation", "disconnected"), ("refresh", "coalesced")],
        )

    def test_unavailable_summary_never_claims_positive_evidence(self):
        summary = unavailable_evidence_summary()
        self.assertFalse(summary["available"])
        self.assertEqual(summary["retainedCount"], 0)
        self.assertEqual(summary["recentEvents"], [])
        self.assertIn("unavailable", summary["disclosure"])


if __name__ == "__main__":
    unittest.main()
