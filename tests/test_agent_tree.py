from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from switchstand.agent_tree import (
    AgentTreeAdapter,
    AgentTreeEvidenceError,
    MAX_DESCENDANT_PAGES,
    MAX_DESCENDANT_RECORDS,
    MAX_PAGINATION_CURSOR_CHARACTERS,
    MAX_PROTOCOL_IDENTITY_CHARACTERS,
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

    def thread_resume(self, thread_id):
        return {"thread": {"id": thread_id}}

    def turn_start_text_native(self, thread_id, text):
        self.starts.append((thread_id, text))
        return {"turn": {"id": "turn-started"}}

    def turn_steer_text(self, thread_id, expected_turn_id, text):
        self.steers.append((thread_id, expected_turn_id, text))
        return {"turnId": expected_turn_id}

    def turn_interrupt(self, thread_id, turn_id):
        self.interrupts.append((thread_id, turn_id))
        return {}


def native_record(
    thread_id: str, *, parent: str | None, session_id: str = "session"
) -> dict[str, object]:
    return {
        "id": thread_id,
        "sessionId": session_id,
        "parentThreadId": parent,
        "createdAt": 0,
        "updatedAt": 0,
        "status": {"type": "idle"},
    }


class GeneratedPaginationClient(FixtureClient):
    def __init__(self, page_factory):
        super().__init__()
        self.page_factory = page_factory

    def thread_list(self, params):
        self.list_requests.append(deepcopy(dict(params)))
        return self.page_factory(len(self.list_requests), params)


class AgentTreeProtocolTests(unittest.TestCase):
    def test_observation_exhausts_pages_and_uses_only_spawned_parent_lineage(self):
        client = FixtureClient()
        observed = AgentTreeAdapter(client).observe_tree("root-1")

        self.assertTrue(observed["paginationComplete"])
        self.assertEqual(observed["pagesRead"], 2)
        self.assertEqual(
            observed["pages"],
            [
                {
                    "page": 1,
                    "requestCursor": None,
                    "resultCount": 1,
                    "nextCursor": "descendants-page-2",
                    "sourceKinds": list(THREAD_SOURCE_KINDS),
                },
                {
                    "page": 2,
                    "requestCursor": "descendants-page-2",
                    "resultCount": 1,
                    "nextCursor": None,
                    "sourceKinds": list(THREAD_SOURCE_KINDS),
                },
            ],
        )
        self.assertEqual(observed["sourceKinds"], list(THREAD_SOURCE_KINDS))
        self.assertEqual(
            [thread["id"] for thread in observed["threads"]],
            ["root-1", "child-1", "grandchild-1"],
        )
        self.assertEqual(observed["threads"][1]["parentThreadId"], "root-1")
        self.assertEqual(observed["threads"][1]["forkedFromId"], "unrelated-history-fork")
        self.assertEqual(
            [thread["sessionId"] for thread in observed["threads"]],
            [
                "opaque-root-session",
                "opaque-child-session",
                "opaque-grandchild-session",
            ],
        )
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
                        "createdAt": 0,
                        "updatedAt": 0.0,
                        "status": {"type": "idle"},
                    }
                ],
                "nextCursor": None,
            }
        ]

        with self.assertRaises(AgentTreeEvidenceError) as raised:
            AgentTreeAdapter(client).observe_tree("root-1")
        self.assertEqual(raised.exception.code, "missing_parent_edge")
        self.assertEqual(raised.exception.phase, "lineage_validation")

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
                        "createdAt": 0,
                        "updatedAt": 0.0,
                        "status": {"type": "notLoaded"},
                    }
                ],
                "nextCursor": None,
            }
        ]

        with self.assertRaises(AgentTreeEvidenceError) as raised:
            AgentTreeAdapter(client).observe_tree("root-1")
        self.assertEqual(raised.exception.code, "missing_intermediate_parent")
        self.assertEqual(raised.exception.phase, "lineage_validation")

    def test_empty_session_id_fails_each_thread_record_closed(self):
        cases = []

        client = FixtureClient()
        client.reads["root-1"]["thread"]["sessionId"] = ""
        cases.append((client, "root_not_found_or_invalid", "root_read"))

        client = FixtureClient()
        client.pages[0]["data"][0]["sessionId"] = ""
        cases.append((client, "invalid_descendant_record", "descendant_list"))

        for client, expected_code, expected_phase in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(AgentTreeEvidenceError) as raised:
                    AgentTreeAdapter(client).observe_tree("root-1")
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(raised.exception.phase, expected_phase)

    def test_root_and_descendant_protocol_timestamps_are_finite_nonnegative_numbers(self):
        invalid_values = (True, -1, float("nan"), float("inf"), float("-inf"))
        for location in ("root", "descendant"):
            for field in ("createdAt", "updatedAt"):
                for invalid in invalid_values:
                    with self.subTest(location=location, field=field, invalid=invalid):
                        client = FixtureClient()
                        record = (
                            client.reads["root-1"]["thread"]
                            if location == "root"
                            else client.pages[0]["data"][0]
                        )
                        record[field] = invalid

                        with self.assertRaises(AgentTreeEvidenceError) as raised:
                            AgentTreeAdapter(client).observe_tree("root-1")

                        self.assertEqual(
                            raised.exception.code, "missing_protocol_timestamp"
                        )
                        self.assertEqual(
                            raised.exception.phase, "timestamp_validation"
                        )

    def test_zero_integer_and_float_protocol_timestamps_are_preserved(self):
        client = FixtureClient()
        client.reads["root-1"]["thread"]["createdAt"] = 0
        client.reads["root-1"]["thread"]["updatedAt"] = 0.0
        client.pages[0]["data"][0]["createdAt"] = 0.0
        client.pages[0]["data"][0]["updatedAt"] = 0

        observed = AgentTreeAdapter(client).observe_tree("root-1")

        self.assertEqual(observed["threads"][0]["createdAt"], 0)
        self.assertEqual(observed["threads"][0]["updatedAt"], 0.0)
        self.assertEqual(observed["threads"][1]["createdAt"], 0.0)
        self.assertEqual(observed["threads"][1]["updatedAt"], 0)

    def test_unique_cursor_page_101_fails_after_exactly_100_requests(self):
        secret = "private-cursor"
        client = GeneratedPaginationClient(
            lambda page, _params: {
                "data": [],
                "nextCursor": f"{secret}-{page}",
            }
        )

        with self.assertRaises(AgentTreeEvidenceError) as raised:
            AgentTreeAdapter(client).observe_tree("root-1")

        self.assertEqual(len(client.list_requests), MAX_DESCENDANT_PAGES)
        self.assertEqual(raised.exception.code, "invalid_pagination")
        self.assertEqual(raised.exception.phase, "descendant_list")
        self.assertNotIn(secret, str(raised.exception))

    def test_record_and_response_page_overflow_fail_before_validation(self):
        record = native_record("repeated-private-id", parent="root-1")
        cases = (
            ([record] * (MAX_DESCENDANT_RECORDS + 1), "record_limit"),
            ([record] * 101, "requested_page_limit"),
        )
        for data, label in cases:
            with self.subTest(label=label):
                client = GeneratedPaginationClient(
                    lambda _page, _params, data=data: {
                        "data": data,
                        "nextCursor": None,
                    }
                )
                with self.assertRaises(AgentTreeEvidenceError) as raised:
                    AgentTreeAdapter(client).observe_tree("root-1")
                self.assertEqual(raised.exception.code, "invalid_pagination")
                self.assertNotIn("repeated-private-id", str(raised.exception))

    def test_overlength_cursor_and_protocol_identities_fail_without_disclosure(self):
        secret = "x" * (MAX_PROTOCOL_IDENTITY_CHARACTERS + 1)
        cursor_client = GeneratedPaginationClient(
            lambda _page, _params: {"data": [], "nextCursor": secret}
        )
        with self.assertRaises(AgentTreeEvidenceError) as raised:
            AgentTreeAdapter(cursor_client).observe_tree("root-1")
        self.assertEqual(raised.exception.code, "invalid_pagination")
        self.assertNotIn(secret, str(raised.exception))

        cases = []
        for field in ("id", "sessionId", "parentThreadId", "forkedFromId"):
            root_client = FixtureClient()
            root_client.reads["root-1"]["thread"][field] = secret
            cases.append((root_client, "root_not_found_or_invalid"))

            descendant_client = FixtureClient()
            descendant_client.pages[0]["data"][0][field] = secret
            cases.append((descendant_client, "invalid_descendant_record"))

        for client, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(AgentTreeEvidenceError) as raised:
                    AgentTreeAdapter(client).observe_tree("root-1")
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn(secret, str(raised.exception))

    def test_exact_identity_cursor_page_and_record_limits_complete(self):
        root_id = "r" * MAX_PROTOCOL_IDENTITY_CHARACTERS
        session_id = "s" * MAX_PROTOCOL_IDENTITY_CHARACTERS
        child_id = "c" * MAX_PROTOCOL_IDENTITY_CHARACTERS
        fork_id = "f" * MAX_PROTOCOL_IDENTITY_CHARACTERS
        cursor = "p" * MAX_PAGINATION_CURSOR_CHARACTERS
        client = FixtureClient()
        client.reads = {
            root_id: {"thread": native_record(root_id, parent=None, session_id=session_id)}
        }
        child = native_record(child_id, parent=root_id, session_id=session_id)
        child["forkedFromId"] = fork_id
        client.pages = [
            {"data": [child], "nextCursor": cursor},
            {"data": [], "nextCursor": None},
        ]

        observed = AgentTreeAdapter(client).observe_tree(root_id)

        self.assertEqual([item["id"] for item in observed["threads"]], [root_id, child_id])
        self.assertEqual(observed["pagesRead"], 2)

        def full_page(page, _params):
            data = [
                native_record(f"child-{page}-{item}", parent="root-1")
                for item in range(100)
            ]
            next_cursor = f"page-{page + 1}" if page < MAX_DESCENDANT_PAGES else None
            return {"data": data, "nextCursor": next_cursor}

        full_client = GeneratedPaginationClient(full_page)
        full = AgentTreeAdapter(full_client).observe_tree("root-1")
        self.assertEqual(len(full["threads"]) - 1, MAX_DESCENDANT_RECORDS)
        self.assertEqual(full["pagesRead"], MAX_DESCENDANT_PAGES)

    def test_native_status_event_projects_exact_supported_cases_and_rejects_others(self):
        adapter = AgentTreeAdapter(FixtureClient())
        cases = (
            ({"type": "active", "activeFlags": ["waitingOnUserInput"]}, None),
            ({"type": "idle"}, None),
            ({"type": "systemError"}, None),
            ({"type": "notLoaded"}, None),
            ({"type": "done"}, "unsupported_status_or_flag"),
            ({"type": "active", "activeFlags": ["invented"]}, "unsupported_status_or_flag"),
        )
        for status, error_code in cases:
            notification = {
                "method": "thread/status/changed",
                "params": {"threadId": "child-1", "status": status},
            }
            with self.subTest(status=status):
                if error_code:
                    with self.assertRaises(AgentTreeEvidenceError) as raised:
                        adapter.status_change(notification)
                    self.assertEqual(raised.exception.code, error_code)
                else:
                    observed = adapter.status_change(notification)
                    self.assertEqual(observed, {"threadId": "child-1", "status": status})
                    self.assertNotIn("done", observed)

    def test_idle_send_starts_normal_turn_without_steering(self):
        client = FixtureClient()
        client.reads["idle-1"] = {
            "thread": {
                "id": "idle-1",
                "sessionId": "idle-1",
                "parentThreadId": None,
                "createdAt": 0,
                "updatedAt": 0,
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
                "createdAt": 0,
                "updatedAt": 0,
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
                        "createdAt": 0,
                        "updatedAt": 0,
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
