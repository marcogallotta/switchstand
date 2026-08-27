from __future__ import annotations

from dataclasses import dataclass
import unittest

from switchstand.native_turns import NativeTurnProjection, project_native_turns


@dataclass(frozen=True)
class OpaqueTarget:
    value: int


def response(target, status="idle", turns=None):
    return {
        "thread": {
            "id": target,
            "status": {"type": status},
            "turns": [] if turns is None else turns,
            "content": "READ-CONTENT-SENTINEL",
        }
    }


class NativeTurnsTests(unittest.TestCase):
    def test_projection_exceptions_fail_closed_without_disclosing_target(self):
        class ExplodingTarget:
            def __eq__(self, other):
                raise RuntimeError("PRIVATE-TARGET-SENTINEL")

        self.assertIsNone(project_native_turns(
            {"thread": {"id": "native", "status": {"type": "idle"}, "turns": []}},
            ExplodingTarget(),
        ))

    def test_projects_each_consistent_known_thread_status(self):
        target = OpaqueTarget(1)
        cases = (
            ("idle", [], NativeTurnProjection("idle", None)),
            ("active", [{"id": "turn-1", "status": "inProgress"}],
                NativeTurnProjection("active", "turn-1")),
            ("systemError", [{"id": "turn-1", "status": "failed"}],
                NativeTurnProjection("systemError", None)),
            ("notLoaded", [{"id": "turn-1", "status": "completed"}],
                NativeTurnProjection("notLoaded", None)),
        )
        for status, turns, expected in cases:
            with self.subTest(status=status):
                self.assertEqual(project_native_turns(response(target, status, turns), target), expected)

    def test_rejects_missing_wrong_or_unknown_thread_evidence(self):
        target = OpaqueTarget(1)
        cases = (
            None,
            [],
            {},
            {"thread": []},
            {"thread": {"id": OpaqueTarget(2), "status": {"type": "idle"}, "turns": []}},
            {"thread": {"id": target, "status": "idle", "turns": []}},
            {"thread": {"id": target, "status": {}, "turns": []}},
            {"thread": {"id": target, "status": {"type": "done"}, "turns": []}},
            {"thread": {"id": target, "status": {"type": "idle"}}},
            {"thread": {"id": target, "status": {"type": "idle"}, "turns": {}}},
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertIsNone(project_native_turns(value, target))

    def test_rejects_malformed_unknown_duplicate_or_oversized_turns(self):
        target = OpaqueTarget(1)
        cases = (
            [None],
            [{}],
            [{"id": "", "status": "completed"}],
            [{"id": 1, "status": "completed"}],
            [{"id": "turn", "status": None}],
            [{"id": "turn", "status": "unknown"}],
            [{"id": "x" * 257, "status": "completed"}],
            [{"id": "same", "status": "completed"},
                {"id": "same", "status": "failed"}],
            [{"id": str(index), "status": "completed"} for index in range(257)],
        )
        for turns in cases:
            with self.subTest(turns=len(turns)):
                self.assertIsNone(project_native_turns(response(target, turns=turns), target))

    def test_accepts_256_unique_turns_and_every_known_turn_status(self):
        target = OpaqueTarget(1)
        statuses = ("inProgress", "completed", "failed", "interrupted")
        turns = [
            {"id": f"turn-{index}", "status": statuses[index % len(statuses)]}
            for index in range(256)
        ]
        turns[0]["status"] = "inProgress"
        for turn in turns[1:]:
            if turn["status"] == "inProgress":
                turn["status"] = "completed"
        self.assertEqual(
            project_native_turns(response(target, "active", turns), target),
            NativeTurnProjection("active", "turn-0"),
        )

    def test_rejects_inconsistent_or_ambiguous_active_turn_state(self):
        target = OpaqueTarget(1)
        cases = (
            response(target, "active", []),
            response(target, "active", [{"id": "one", "status": "completed"}]),
            response(target, "active", [
                {"id": "one", "status": "inProgress"},
                {"id": "two", "status": "inProgress"},
            ]),
            response(target, "idle", [{"id": "one", "status": "inProgress"}]),
            response(target, "systemError", [{"id": "one", "status": "inProgress"}]),
            response(target, "notLoaded", [{"id": "one", "status": "inProgress"}]),
        )
        for value in cases:
            with self.subTest(status=value["thread"]["status"]):
                self.assertIsNone(project_native_turns(value, target))


if __name__ == "__main__":
    unittest.main()
