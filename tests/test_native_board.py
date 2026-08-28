from __future__ import annotations

import json
import threading
from typing import Any
import unittest

from switchstand.current_target import ExactCurrentTarget
from switchstand.native_board import NativeBoard
from switchstand.native_contracts import (
    NativeBrowserSelectionResult,
    NativeSelectionPair,
)


def native_thread(
    thread_id: str,
    *,
    parent: str | None = None,
    status: str = "active",
    updated: float = 100.0,
) -> dict[str, Any]:
    return {
        "id": thread_id,
        "sessionId": f"secret-session-{thread_id}",
        "parentThreadId": parent,
        "preview": f"secret prompt {thread_id}",
        "name": f"secret name {thread_id}",
        "source": "cli" if parent is None else {"subAgent": "review", "secret": "drop"},
        "createdAt": 90.0,
        "updatedAt": updated,
        "status": {"type": status, **({"activeFlags": ["waitingOnUserInput"]} if status == "active" else {})},
    }


class FakeClient:
    def __init__(self, root: dict[str, Any], pages: list[dict[str, Any]]) -> None:
        self.root = root
        self.pages = pages
        self.list_requests: list[dict[str, Any]] = []
        self.closed = False

    def thread_read(self, thread_id: str, *, include_turns: bool = True):
        self.read = (thread_id, include_turns)
        return {"thread": self.root}

    def thread_list(self, params):
        request = dict(params)
        self.list_requests.append(request)
        page = 0 if "cursor" not in request else 1
        return self.pages[page]

    def stop_request(self, method, params, **_limits):
        self.turn_request = (method, dict(params))
        return ("ok", {"data": [], "nextCursor": None})

    def close(self):
        self.closed = True


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


class ClientFactory:
    def __init__(self, *values: FakeClient | Exception) -> None:
        self.values = list(values)

    def __call__(self) -> FakeClient:
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def target_error_code(value: dict[str, Any] | ExactCurrentTarget) -> Any:
    if not isinstance(value, dict):
        raise AssertionError("expected a frozen safe error")
    return value["code"]


def selection_pair(value: NativeBrowserSelectionResult) -> NativeSelectionPair:
    pair = value["selection"]
    if pair is None:
        raise AssertionError("expected a current selection pair")
    return pair


def selection_error_code(value: NativeBrowserSelectionResult) -> str:
    snapshot = value["snapshot"]
    if "code" not in snapshot:
        raise AssertionError("expected a frozen safe selection error")
    return snapshot["code"]


class FailSecondPageClient(FakeClient):
    def thread_list(self, params):
        if "cursor" in params:
            self.list_requests.append(dict(params))
            raise OSError("private pagination failure")
        return super().thread_list(params)


def client_with_status(status: str = "active", *, updated: float = 100.0) -> FakeClient:
    return FakeClient(
        native_thread("raw-root", status=status, updated=updated),
        [
            {
                "data": [native_thread("raw-child", parent="raw-root", status=status, updated=updated)],
                "nextCursor": "secret-cursor",
            },
            {
                "data": [native_thread("raw-grandchild", parent="raw-child", status="idle", updated=updated)],
                "nextCursor": None,
            },
        ],
    )


class NativeBoardTests(unittest.TestCase):
    def test_complete_pass_forces_state_db_only_and_projects_no_sensitive_values(self):
        client = client_with_status()
        clock = Clock()
        board = NativeBoard(lambda: client, "raw-root", wall_clock=clock, monotonic=clock)

        board.poll_once()
        result = board.snapshot()

        self.assertEqual(client.read, ("raw-root", False))
        self.assertTrue(client.closed)
        self.assertEqual(len(client.list_requests), 2)
        self.assertTrue(all(request["useStateDbOnly"] is True for request in client.list_requests))
        self.assertEqual([agent["depth"] for agent in result["agents"]], [0, 1, 2])
        self.assertEqual(result["agents"][1]["sourceKind"], "subAgent")
        self.assertEqual(result["agents"][1]["sourceDetail"], "review")
        self.assertEqual(result["agents"][1]["parentRef"], "agent-1")
        self.assertEqual(result["agents"][0]["updatedAgeSeconds"], 0.0)
        emitted = json.dumps(result)
        for secret in ("raw-root", "raw-child", "secret-session", "secret prompt", "secret name", "secret-cursor"):
            self.assertNotIn(secret, emitted)

    def test_active_time_resets_on_gap_and_trail_is_capped(self):
        clock = Clock()
        factory = ClientFactory(
            client_with_status(),
            client_with_status(updated=105),
            OSError("private socket path"),
            client_with_status(updated=109),
            client_with_status("idle", updated=110),
        )
        board = NativeBoard(factory, "raw-root", wall_clock=clock, monotonic=clock, trail_limit=2)

        board.poll_once()
        clock.value = 105
        board.poll_once()
        self.assertEqual(board.snapshot()["agents"][0]["activeObservedSeconds"], 5.0)

        clock.value = 107
        board.poll_once()
        failed = board.snapshot()
        self.assertFalse(failed["observation"]["available"])
        self.assertTrue(failed["observation"]["historical"])
        self.assertEqual(failed["observation"]["errorCode"], "native_observation_unavailable")
        self.assertEqual(failed["agents"][0]["activeObservedSeconds"], 0.0)
        self.assertNotIn("private socket path", json.dumps(failed))

        clock.value = 109
        board.poll_once()
        self.assertEqual(board.snapshot()["agents"][0]["activeObservedSeconds"], 0.0)
        clock.value = 110
        board.poll_once()
        recovered = board.snapshot()
        self.assertEqual(recovered["agents"][0]["status"], "idle")
        self.assertLessEqual(len(recovered["trail"]), 2)
        self.assertTrue(any("status" in entry["changes"] for entry in recovered["trail"]))

    def test_inactive_status_drops_untrusted_active_flags(self):
        client = client_with_status("idle")
        client.root["status"]["activeFlags"] = ["secret-inactive-flag"]
        board = NativeBoard(lambda: client, "raw-root")

        board.poll_once()
        result = board.snapshot()

        self.assertEqual(result["agents"][0]["activeFlags"], [])
        self.assertNotIn("secret-inactive-flag", json.dumps(result))

    def test_invalid_complete_pass_retains_last_good_projection(self):
        good = client_with_status()
        invalid = client_with_status()
        del invalid.pages[0]["data"][0]["updatedAt"]
        board = NativeBoard(ClientFactory(good, invalid), "raw-root")

        board.poll_once()
        before = board.snapshot()["agents"]
        identities_before = dict(board._target_identities)
        reverse_before = dict(board._native_ids_by_target)
        board.poll_once()
        after = board.snapshot()

        for agent in before:
            agent.pop("updatedAgeSeconds")
        retained = after["agents"]
        for agent in retained:
            agent.pop("updatedAgeSeconds")
        self.assertEqual(before, retained)
        self.assertEqual(board._target_identities, identities_before)
        self.assertEqual(board._native_ids_by_target, reverse_before)
        self.assertFalse(after["observation"]["connected"])

    def test_nonfinite_later_pass_preserves_exact_last_good_board_and_selection_identity(self):
        first = client_with_status(updated=100.0)
        second = client_with_status(updated=105.0)
        invalid = client_with_status(updated=106.0)
        invalid.pages[0]["data"][0]["updatedAt"] = float("nan")
        clock = Clock()
        board = NativeBoard(
            ClientFactory(first, second, invalid),
            "raw-root",
            wall_clock=clock,
            monotonic=clock,
        )

        board.poll_once()
        clock.value = 105.0
        board.poll_once()
        before = board.snapshot()
        projection_before = board._projection
        before_selection = board.browser_selection(
            "agent-2", now=105.0, maximum_observation_age_seconds=5.0
        )
        identities_before = dict(board._target_identities)
        reverse_before = dict(board._native_ids_by_target)

        board.poll_once()
        after = board.snapshot()
        after_selection = board.browser_selection(
            "agent-2", now=105.0, maximum_observation_age_seconds=5.0
        )

        self.assertEqual(board._projection, projection_before)
        self.assertEqual(after["agents"], before["agents"])
        self.assertEqual(after["trail"], before["trail"])
        self.assertEqual(
            after["observation"]["completedAt"],
            before["observation"]["completedAt"],
        )
        self.assertFalse(after["observation"]["connected"])
        self.assertFalse(after["observation"]["available"])
        self.assertTrue(after["observation"]["historical"])
        self.assertEqual(board._target_identities, identities_before)
        self.assertEqual(board._native_ids_by_target, reverse_before)
        self.assertEqual(after_selection["selection"], before_selection["selection"])
        self.assertEqual(
            selection_error_code(after_selection), "APP_SERVER_DISCONNECTED"
        )
        json.dumps(after, allow_nan=False)

    def test_stop_resolution_requires_current_connected_active_board_evidence(self):
        board = NativeBoard(ClientFactory(client_with_status(), OSError("gone")), "raw-root")
        board.poll_once()
        self.assertEqual(board._resolve_present("agent-1"), "raw-root")
        self.assertIsNone(board._resolve_present("unknown"))
        board.poll_once()
        self.assertIsNone(board._resolve_present("agent-1"))

    def test_run_identity_is_stable_within_run_and_old_pair_cannot_retarget(self):
        clock = Clock()
        first_board = NativeBoard(
            ClientFactory(client_with_status(), client_with_status(updated=101)),
            "raw-root",
            wall_clock=clock,
            monotonic=clock,
        )
        first_board.poll_once()
        first_shape = first_board.browser_selection(
            "agent-1", now=100.0, maximum_observation_age_seconds=5.0
        )
        old_pair = selection_pair(first_shape)
        first_target = first_board.resolve_current_target(
            old_pair, now=100.0, maximum_observation_age_seconds=5.0
        )
        self.assertIsInstance(first_target, ExactCurrentTarget)

        clock.value = 101.0
        first_board.poll_once()
        second_shape = first_board.browser_selection(
            "agent-1", now=101.0, maximum_observation_age_seconds=5.0
        )
        second_target = first_board.resolve_current_target(
            old_pair, now=101.0, maximum_observation_age_seconds=5.0
        )
        self.assertEqual(
            selection_pair(second_shape)["observationRunRef"], old_pair["observationRunRef"]
        )
        self.assertEqual(second_target, first_target)

        new_board = NativeBoard(
            lambda: client_with_status(), "raw-root", wall_clock=clock, monotonic=clock
        )
        new_board.poll_once()
        new_pair = selection_pair(new_board.browser_selection(
            "agent-1", now=101.0, maximum_observation_age_seconds=5.0
        ))
        self.assertNotEqual(new_pair["observationRunRef"], old_pair["observationRunRef"])
        self.assertEqual(
            target_error_code(new_board.resolve_current_target(
                old_pair, now=101.0, maximum_observation_age_seconds=5.0
            )),
            "INVALID_AGENT_REF",
        )
        new_target = new_board.resolve_current_target(
            new_pair, now=101.0, maximum_observation_age_seconds=5.0
        )
        self.assertIsInstance(new_target, ExactCurrentTarget)
        self.assertNotEqual(new_target, first_target)

        invalid = new_board.browser_selection(
            "raw-root",
            now=101.0,
            maximum_observation_age_seconds=5.0,
        )
        self.assertIsNone(invalid["selection"])
        self.assertEqual(selection_error_code(invalid), "INVALID_AGENT_REF")
        self.assertNotIn("raw-root", json.dumps(invalid))

    def test_incomplete_pagination_retains_complete_projection_time_run_and_target_index(self):
        clock = Clock()
        partial = FailSecondPageClient(
            native_thread("raw-root", updated=105.0),
            [
                {
                    "data": [
                        native_thread("raw-child", parent="raw-root", updated=105.0),
                        native_thread("raw-partial", parent="raw-root", updated=105.0),
                    ],
                    "nextCursor": "secret-cursor",
                }
            ],
        )
        board = NativeBoard(
            ClientFactory(client_with_status(), partial),
            "raw-root",
            wall_clock=clock,
            monotonic=clock,
        )
        board.poll_once()
        before = board.snapshot()
        pair = selection_pair(board.browser_selection(
            "agent-1", now=100.0, maximum_observation_age_seconds=10.0
        ))
        target = board.resolve_current_target(
            pair, now=100.0, maximum_observation_age_seconds=10.0
        )
        records_before = tuple(board._target_records)
        identities_before = dict(board._target_identities)
        reverse_before = dict(board._native_ids_by_target)

        clock.value = 105.0
        board.poll_once()
        failed = board.snapshot()
        failed_shape = board.browser_selection(
            "agent-1", now=105.0, maximum_observation_age_seconds=10.0
        )

        self.assertFalse(failed["observation"]["connected"])
        self.assertTrue(failed["observation"]["historical"])
        self.assertEqual(failed["observation"]["completedAt"], before["observation"]["completedAt"])
        self.assertEqual(failed["trail"], before["trail"])
        self.assertEqual(tuple(board._target_records), records_before)
        self.assertEqual(board._target_identities, identities_before)
        self.assertEqual(board._native_ids_by_target, reverse_before)
        self.assertEqual(failed_shape["selection"], pair)
        self.assertEqual(selection_error_code(failed_shape), "APP_SERVER_DISCONNECTED")
        self.assertEqual(
            target_error_code(board.resolve_current_target(
                pair, now=105.0, maximum_observation_age_seconds=10.0
            )),
            "APP_SERVER_DISCONNECTED",
        )
        self.assertIsInstance(target, ExactCurrentTarget)
        self.assertNotIn("raw-partial", json.dumps(failed))

    def test_absent_known_agent_and_stale_or_closed_board_fail_closed(self):
        clock = Clock()
        root_only = FakeClient(
            native_thread("raw-root", updated=101.0),
            [{"data": [], "nextCursor": None}],
        )
        board = NativeBoard(
            ClientFactory(client_with_status(), root_only),
            "raw-root",
            wall_clock=clock,
            monotonic=clock,
            maximum_observation_age_seconds=1.0,
        )
        board.poll_once()
        child_pair = selection_pair(board.browser_selection(
            "agent-2", now=100.0, maximum_observation_age_seconds=1.0
        ))
        clock.value = 101.0
        board.poll_once()
        self.assertEqual(
            target_error_code(board.resolve_current_target(
                child_pair, now=101.0, maximum_observation_age_seconds=1.0
            )),
            "AGENT_NOT_PRESENT",
        )
        root_pair = selection_pair(board.browser_selection(
            "agent-1", now=101.0, maximum_observation_age_seconds=1.0
        ))
        self.assertIsInstance(
            board.resolve_current_target(
                root_pair, now=102.0, maximum_observation_age_seconds=1.0
            ),
            ExactCurrentTarget,
        )
        self.assertEqual(
            target_error_code(board.resolve_current_target(
                root_pair, now=102.001, maximum_observation_age_seconds=1.0
            )),
            "OBSERVATION_STALE",
        )
        clock.value = 102.001
        self.assertIsNone(board._resolve_present("agent-1"))
        board.close()
        self.assertEqual(
            target_error_code(board.resolve_current_target(
                root_pair, now=101.0, maximum_observation_age_seconds=10.0
            )),
            "APP_SERVER_DISCONNECTED",
        )

    def test_private_target_operation_binds_only_latest_connected_fresh_record(self):
        clock = Clock()
        root_only = FakeClient(
            native_thread("raw-root", updated=101.0),
            [{"data": [], "nextCursor": None}],
        )
        board = NativeBoard(
            ClientFactory(client_with_status(), root_only),
            "raw-root",
            wall_clock=clock,
            monotonic=clock,
            maximum_observation_age_seconds=1.0,
        )
        board.poll_once()
        pair = selection_pair(board.browser_selection(
            "agent-2", now=100.0, maximum_observation_age_seconds=1.0
        ))
        target = board.resolve_current_target(
            pair, now=100.0, maximum_observation_age_seconds=1.0
        )
        self.assertIsInstance(target, ExactCurrentTarget)
        observed: list[str] = []
        operation = lambda native_id: observed.append(native_id) or "called"

        self.assertEqual(board._with_current_native_target(target, operation), "called")
        self.assertEqual(observed, ["raw-child"])

        clock.value = 101.0
        board.poll_once()
        self.assertIsNone(board._with_current_native_target(target, operation))
        self.assertEqual(observed, ["raw-child"])

        root_pair = selection_pair(board.browser_selection(
            "agent-1", now=101.0, maximum_observation_age_seconds=1.0
        ))
        root_target = board.resolve_current_target(
            root_pair, now=101.0, maximum_observation_age_seconds=1.0
        )
        clock.value = 102.001
        self.assertIsNone(board._with_current_native_target(root_target, operation))
        self.assertEqual(observed, ["raw-child"])

    def test_private_target_operation_does_not_hold_board_lock_during_io(self):
        clock = Clock()
        board = NativeBoard(
            lambda: client_with_status(),
            "raw-root",
            wall_clock=clock,
            monotonic=clock,
        )
        board.poll_once()
        pair = selection_pair(board.browser_selection(
            "agent-1", now=100.0, maximum_observation_age_seconds=5.0
        ))
        target = board.resolve_current_target(
            pair, now=100.0, maximum_observation_age_seconds=5.0
        )
        entered = threading.Event()
        release = threading.Event()
        operation_done = threading.Event()

        def delayed_operation(native_id: str) -> str:
            self.assertEqual(native_id, "raw-root")
            entered.set()
            release.wait(2)
            return "done"

        def run_operation() -> None:
            board._with_current_native_target(target, delayed_operation)
            operation_done.set()

        worker = threading.Thread(target=run_operation)
        worker.start()
        self.assertTrue(entered.wait(1))
        try:
            snapshot_done = threading.Event()
            snapshot_thread = threading.Thread(
                target=lambda: (board.snapshot(), snapshot_done.set())
            )
            snapshot_thread.start()
            self.assertTrue(snapshot_done.wait(1))
            snapshot_thread.join(1)
        finally:
            release.set()
            worker.join(1)
        self.assertTrue(operation_done.is_set())

if __name__ == "__main__":
    unittest.main()
