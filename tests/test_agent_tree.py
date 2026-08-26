from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from switchstand.agent_tree import (
    AgentTreeAdapter,
    AgentTreeEvidenceError,
    NATIVE_THREAD_STATUS_TYPES,
    THREAD_SOURCE_KINDS,
)


FIXTURES = Path(__file__).parent / "fixtures" / "app_server"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureClient:
    def __init__(self) -> None:
        self.reads = {"root-1": fixture("thread_read_root.json")}
        self.pages = [
            fixture("thread_list_descendants_page_1.json"),
            fixture("thread_list_descendants_page_2.json"),
        ]
        self.list_requests = []
        self.starts = []
        self.steers = []
        self.interrupts = []

    def thread_read(self, thread_id, *, include_turns=True):
        return deepcopy(self.reads[thread_id])

    def thread_list(self, params):
        self.list_requests.append(deepcopy(dict(params)))
        return deepcopy(self.pages[len(self.list_requests) - 1])

    def turn_start_text_native(self, thread_id, text):
        self.starts.append((thread_id, text))
        return {"turn": {"id": "turn-started"}}

    def turn_steer_text(self, thread_id, expected_turn_id, text):
        self.steers.append((thread_id, expected_turn_id, text))
        return {"turnId": expected_turn_id}

    def turn_interrupt(self, thread_id, turn_id):
        self.interrupts.append((thread_id, turn_id))
        return {}


class AgentTreeProtocolTests(unittest.TestCase):
    def test_observation_exhausts_pages_and_uses_only_spawned_parent_lineage(self):
        client = FixtureClient()
        observed = AgentTreeAdapter(client).observe_tree("root-1")

        self.assertTrue(observed["paginationComplete"])
        self.assertEqual(observed["pagesRead"], 2)
        self.assertEqual(observed["sourceKinds"], list(THREAD_SOURCE_KINDS))
        self.assertEqual(
            [thread["id"] for thread in observed["threads"]],
            ["root-1", "child-1", "grandchild-1"],
        )
        self.assertEqual(observed["threads"][1]["parentThreadId"], "root-1")
        self.assertEqual(observed["threads"][1]["forkedFromId"], "unrelated-history-fork")
        self.assertNotEqual(
            observed["threads"][1]["parentThreadId"],
            observed["threads"][1]["forkedFromId"],
        )
        self.assertNotIn("cursor", client.list_requests[0])
        self.assertEqual(client.list_requests[1]["cursor"], "descendants-page-2")
        for request in client.list_requests:
            self.assertEqual(request["ancestorThreadId"], "root-1")
            self.assertEqual(request["sourceKinds"], list(THREAD_SOURCE_KINDS))
            self.assertEqual(request["limit"], 100)

    def test_generic_fork_history_is_not_accepted_as_spawned_lineage(self):
        client = FixtureClient()
        client.pages = [
            {
                "data": [
                    {
                        "id": "fork-only",
                        "sessionId": "root-1",
                        "forkedFromId": "root-1",
                        "parentThreadId": None,
                        "status": {"type": "idle"},
                    }
                ],
                "nextCursor": None,
            }
        ]

        with self.assertRaisesRegex(AgentTreeEvidenceError, "no spawned parent"):
            AgentTreeAdapter(client).observe_tree("root-1")

    def test_missing_intermediate_parent_fails_closed(self):
        client = FixtureClient()
        client.pages = [
            {
                "data": [
                    {
                        "id": "grandchild-only",
                        "sessionId": "root-1",
                        "forkedFromId": None,
                        "parentThreadId": "missing-child",
                        "status": {"type": "notLoaded"},
                    }
                ],
                "nextCursor": None,
            }
        ]

        with self.assertRaisesRegex(AgentTreeEvidenceError, "missing parent missing-child"):
            AgentTreeAdapter(client).observe_tree("root-1")

    def test_descendant_from_another_native_session_fails_closed(self):
        client = FixtureClient()
        client.pages = [deepcopy(client.pages[0])]
        client.pages[0]["data"][0]["sessionId"] = "different-root"
        client.pages[0]["nextCursor"] = None

        with self.assertRaisesRegex(AgentTreeEvidenceError, "does not share root session"):
            AgentTreeAdapter(client).observe_tree("root-1")

    def test_native_status_event_is_preserved_and_idle_is_not_done(self):
        adapter = AgentTreeAdapter(FixtureClient())
        observed = adapter.status_change(fixture("thread_status_changed.json"))

        self.assertEqual(
            observed,
            {
                "threadId": "child-1",
                "status": {"type": "active", "activeFlags": ["waitingOnUserInput"]},
            },
        )
        self.assertEqual(
            NATIVE_THREAD_STATUS_TYPES,
            {"active", "idle", "systemError", "notLoaded"},
        )
        self.assertNotIn("done", NATIVE_THREAD_STATUS_TYPES)

    def test_idle_send_starts_normal_turn_without_steering(self):
        client = FixtureClient()
        client.reads["idle-1"] = {
            "thread": {
                "id": "idle-1",
                "sessionId": "idle-1",
                "parentThreadId": None,
                "status": {"type": "idle"},
                "turns": [],
            }
        }

        result = AgentTreeAdapter(client).send_text("idle-1", "normal message")

        self.assertEqual(
            result, {"mode": "start", "threadId": "idle-1", "turnId": "turn-started"}
        )
        self.assertEqual(client.starts, [("idle-1", "normal message")])
        self.assertEqual(client.steers, [])

    def test_active_send_steers_the_exact_in_progress_turn(self):
        client = FixtureClient()
        client.reads["active-1"] = {
            "thread": {
                "id": "active-1",
                "sessionId": "active-1",
                "parentThreadId": None,
                "status": {"type": "active", "activeFlags": []},
                "turns": [
                    {"id": "turn-old", "status": "completed", "items": []},
                    {"id": "turn-exact", "status": "inProgress", "items": []},
                ],
            }
        }

        result = AgentTreeAdapter(client).send_text("active-1", "focus on tests")

        self.assertEqual(
            result, {"mode": "steer", "threadId": "active-1", "turnId": "turn-exact"}
        )
        self.assertEqual(client.starts, [])
        self.assertEqual(client.steers, [("active-1", "turn-exact", "focus on tests")])

    def test_unavailable_or_ambiguous_send_does_not_fall_back(self):
        for status, turns in (
            ({"type": "notLoaded"}, []),
            ({"type": "systemError"}, []),
            ({"type": "active", "activeFlags": []}, []),
        ):
            with self.subTest(status=status):
                client = FixtureClient()
                client.reads["target"] = {
                    "thread": {
                        "id": "target",
                        "sessionId": "target",
                        "parentThreadId": None,
                        "status": status,
                        "turns": turns,
                    }
                }
                with self.assertRaises(AgentTreeEvidenceError):
                    AgentTreeAdapter(client).send_text("target", "must not reroute")
                self.assertEqual(client.starts, [])
                self.assertEqual(client.steers, [])

    def test_stop_targets_exact_native_thread_and_turn(self):
        client = FixtureClient()
        AgentTreeAdapter(client).stop("thread-exact", "turn-exact")
        self.assertEqual(client.interrupts, [("thread-exact", "turn-exact")])


if __name__ == "__main__":
    unittest.main()
