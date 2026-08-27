from __future__ import annotations

from copy import deepcopy
import json
import unittest

from switchstand.native_projection import project_complete_tree


def thread(
    thread_id: str,
    *,
    parent: str | None = None,
    status: str = "active",
    updated_at: float = 100.0,
) -> dict[str, object]:
    return {
        "id": thread_id,
        "parentThreadId": parent,
        "source": "cli" if parent is None else {"subAgent": "review", "private": "drop"},
        "createdAt": 90.0,
        "updatedAt": updated_at,
        "status": {
            "type": status,
            **({"activeFlags": ["waitingOnUserInput"]} if status == "active" else {}),
        },
    }


class NativeProjectionTests(unittest.TestCase):
    def test_identical_complete_inputs_and_prior_state_are_deterministic_and_pure(self):
        threads = [thread("raw-root"), thread("raw-child", parent="raw-root")]
        original = deepcopy(threads)
        arguments = {
            "prior_projection": None,
            "completed_at": 100.0,
            "completed_monotonic": 50.0,
            "trail_limit": 50,
        }
        first = project_complete_tree(threads, **arguments)
        second = project_complete_tree(threads, **arguments)

        self.assertEqual(first, second)
        self.assertEqual(threads, original)
        self.assertEqual([agent["depth"] for agent in first.agents], [0, 1])
        self.assertEqual(first.agents[1]["parentRef"], "agent-1")
        self.assertEqual(first.agents[1]["sourceKind"], "subAgent")
        self.assertEqual(first.agents[1]["sourceDetail"], "review")
        for private_field in ("labels", "active_since", "endpoints", "targets"):
            self.assertFalse(hasattr(first, private_field))
        self.assertFalse(hasattr(first, "__dict__"))
        self.assertNotIn("raw-root", repr(first))
        self.assertNotIn("raw-child", repr(first))
        continuation = object.__getattribute__(first, "_NativeProjection__state")
        self.assertNotIn("raw-root", repr(continuation))
        self.assertNotIn("raw-child", repr(continuation))
        self.assertNotIn("raw-root", json.dumps({"agents": first.agents, "trail": first.trail}))

    def test_explicit_prior_state_preserves_activity_and_trail_semantics(self):
        first = project_complete_tree(
            [thread("root"), thread("child", parent="root", status="idle")],
            prior_projection=None,
            completed_at=100.0,
            completed_monotonic=50.0,
            trail_limit=2,
        )
        second = project_complete_tree(
            [thread("root", updated_at=105.0), thread("child", parent="root", status="active")],
            prior_projection=first,
            completed_at=105.0,
            completed_monotonic=55.0,
            trail_limit=2,
        )

        self.assertEqual(second.agents[0]["activeObservedSeconds"], 5.0)
        self.assertEqual(second.agents[1]["activeObservedSeconds"], 0.0)
        self.assertEqual(second.agents[1]["status"], "active")
        self.assertEqual(second.agents[1]["activeFlags"], ["waitingOnUserInput"])
        self.assertLessEqual(len(second.trail), 2)
        self.assertTrue(any("status" in entry["changes"] for entry in second.trail))

    def test_idle_remains_idle_and_drops_inactive_flags(self):
        idle = thread("root", status="idle")
        idle["status"] = {"type": "idle", "activeFlags": ["private-flag"]}
        result = project_complete_tree(
            [idle],
            prior_projection=None,
            completed_at=100.0,
            completed_monotonic=50.0,
            trail_limit=50,
        )
        self.assertEqual(result.agents[0]["status"], "idle")
        self.assertEqual(result.agents[0]["activeFlags"], [])
        self.assertNotIn("done", result.agents[0])
        self.assertNotIn("completed", result.agents[0])


if __name__ == "__main__":
    unittest.main()
