from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from switchstand.native_observer import ERROR_CODE, NativeObserver


FIXTURES = Path(__file__).parent / "fixtures" / "app_server"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClient:
    def __init__(self, *, child_status: dict | None = None, invalid_timestamp: bool = False) -> None:
        self.root = fixture("thread_read_root.json")
        self.pages = [
            fixture("thread_list_descendants_page_1.json"),
            fixture("thread_list_descendants_page_2.json"),
        ]
        if child_status is not None:
            self.pages[0]["data"][0]["status"] = child_status
            self.pages[0]["data"][0]["updatedAt"] += 10
        if invalid_timestamp:
            self.root["thread"]["updatedAt"] = "private-bad-value"
        self.reads = []
        self.list_requests = []
        self.closed = False

    def thread_read(self, thread_id, *, include_turns=True):
        self.reads.append((thread_id, include_turns))
        return deepcopy(self.root)

    def thread_list(self, params):
        self.list_requests.append(deepcopy(dict(params)))
        return deepcopy(self.pages[len(self.list_requests) - 1])

    def close(self):
        self.closed = True


class Factory:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class NativeObserverTests(unittest.TestCase):
    def test_projects_private_tree_and_forces_state_db_only_on_every_page(self):
        active = FakeClient()
        still_active = FakeClient()
        idle = FakeClient(child_status={"type": "idle"})
        times = iter([100.0, 105.0, 110.0])
        observer = NativeObserver(
            Factory([active, still_active, idle]), "root-1", clock=lambda: next(times), trail_limit=3
        )

        observer.observe_once()
        first = observer.snapshot()
        observer.observe_once()
        second = observer.snapshot()
        observer.observe_once()
        third = observer.snapshot()

        self.assertEqual(active.reads, [("root-1", False)])
        self.assertEqual(idle.reads, [("root-1", False)])
        for client in (active, still_active, idle):
            self.assertTrue(client.closed)
            self.assertEqual(len(client.list_requests), 2)
            self.assertTrue(all(request["useStateDbOnly"] is True for request in client.list_requests))
            self.assertNotIn("cursor", client.list_requests[0])
            self.assertEqual(client.list_requests[1]["cursor"], "descendants-page-2")
        self.assertEqual([item["label"] for item in first["threads"]], ["Root", "Agent 1", "Agent 2"])
        self.assertEqual([item["depth"] for item in first["threads"]], [0, 1, 2])
        self.assertEqual(first["threads"][1]["source"], "subAgent:thread_spawn")
        self.assertEqual(first["threads"][1]["status"]["type"], "active")
        self.assertEqual(first["threads"][1]["activeObservedSeconds"], 0)
        self.assertEqual(first["differences"], [])
        self.assertEqual(second["threads"][1]["activeObservedSeconds"], 5)
        self.assertEqual(third["threads"][1]["status"]["type"], "idle")
        self.assertIsNone(third["threads"][1]["activeObservedSeconds"])
        self.assertLessEqual(len(third["differences"]), 3)
        serialized = json.dumps(third)
        for private in (
            "root-1",
            "child-1",
            "grandchild-1",
            "opaque-root-session",
            "Switchstand checkpoint",
            "Investigate the real agent tree",
            "unrelated-history-fork",
        ):
            self.assertNotIn(private, serialized)

    def test_failure_retains_historical_pass_and_resets_active_duration_on_reconnect(self):
        first_client = FakeClient()
        recovered_client = FakeClient()
        factory = Factory([first_client, OSError("private socket path"), recovered_client])
        times = iter([100.0, 130.0])
        observer = NativeObserver(factory, "root-1", clock=lambda: next(times))

        observer.observe_once()
        observer.observe_once()
        failed = observer.snapshot()
        self.assertFalse(failed["observation"]["connected"])
        self.assertTrue(failed["observation"]["historical"])
        self.assertEqual(failed["observation"]["errorCode"], ERROR_CODE)
        self.assertEqual(failed["passSequence"], 1)
        self.assertEqual(len(failed["threads"]), 3)
        self.assertNotIn("private socket path", json.dumps(failed))

        observer.observe_once()
        recovered = observer.snapshot()
        self.assertTrue(recovered["observation"]["connected"])
        self.assertFalse(recovered["observation"]["historical"])
        self.assertEqual(recovered["passSequence"], 2)
        self.assertEqual(recovered["threads"][1]["activeObservedSeconds"], 0)
        self.assertEqual(factory.calls, 3)

    def test_initial_invalid_evidence_fails_closed_without_leaking_protocol_values(self):
        observer = NativeObserver(Factory([FakeClient(invalid_timestamp=True)]), "root-1")

        observer.observe_once()
        state = observer.snapshot()

        self.assertEqual(state["threads"], [])
        self.assertFalse(state["observation"]["connected"])
        self.assertFalse(state["observation"]["historical"])
        self.assertEqual(state["observation"]["errorCode"], ERROR_CODE)
        self.assertNotIn("private-bad-value", json.dumps(state))


if __name__ == "__main__":
    unittest.main()
