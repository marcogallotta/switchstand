from __future__ import annotations

import json
import http.client
from pathlib import Path
import threading
from typing import Any, cast
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from switchstand.current_target import ExactCurrentTarget
from switchstand.native_board import NativeBoard
from switchstand.native_contracts import NativeBrowserSelectionResult, NativeSelectionPair
from switchstand.service import PACKAGE_ROOT, Server, _loopback


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
        board.poll_once()
        after = board.snapshot()

        for agent in before:
            agent.pop("updatedAgeSeconds")
            agent["activeObservedSeconds"] = 0.0
        retained = after["agents"]
        for agent in retained:
            agent.pop("updatedAgeSeconds")
        self.assertEqual(retained, before)
        self.assertFalse(after["observation"]["connected"])

    def test_stop_resolution_requires_current_connected_active_board_evidence(self):
        board = NativeBoard(ClientFactory(client_with_status(), OSError("gone")), "raw-root")
        board.poll_once()
        self.assertEqual(board._resolve_active("agent-1"), "raw-root")
        self.assertIsNone(board._resolve_active("unknown"))
        board.poll_once()
        self.assertIsNone(board._resolve_active("agent-1"))

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
        self.assertIsNone(board._resolve_active("agent-1"))
        board.close()
        self.assertEqual(
            target_error_code(board.resolve_current_target(
                root_pair, now=101.0, maximum_observation_age_seconds=10.0
            )),
            "APP_SERVER_DISCONNECTED",
        )

    def test_http_api_serves_native_snapshot_and_rejects_controls(self):
        board = NativeBoard(lambda: client_with_status(), "raw-root")
        board.poll_once()
        server = Server(("127.0.0.1", 0), board, Path(PACKAGE_ROOT / "static"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(f"{base}/api/workbench") as response:
                self.assertEqual(json.load(response)["mode"], "native")
            with self.assertRaises(HTTPError) as raised:
                urlopen(Request(f"{base}/api/workbench/reconcile", data=b"{}", method="POST"))
            self.assertEqual(raised.exception.code, 404)
            self.assertEqual(json.load(raised.exception)["error"], "operation_unavailable_in_native_mode")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_native_control_http_boundary_rejects_simple_and_cross_origin_requests(self):
        self.assertTrue(_loopback("127.0.0.1:4180"))
        self.assertFalse(_loopback("0.0.0.0:4180"))
        board = NativeBoard(lambda: client_with_status(), "raw-root")
        board.poll_once()

        class Stopper:
            def __init__(self):
                self.calls = []

            def prepare(self, value):
                self.calls.append(value)
                return {"code": "prepared", "agentRef": value, "confirmationRef": "opaque"}

        stopper = Stopper()
        board._stopper = cast(Any, stopper)
        server = Server(("127.0.0.1", 0), board, Path(PACKAGE_ROOT / "static"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            valid = {
                "Host": f"127.0.0.1:{port}",
                "Origin": f"http://127.0.0.1:{port}",
                "Content-Type": "application/json",
                "X-Switchstand-Control": "native-stop-v1",
            }
            cases = [
                {},
                {**valid, "Content-Type": "text/plain"},
                {**valid, "X-Switchstand-Control": "wrong"},
                {**valid, "Host": "example.com"},
                {**valid, "Host": f"user@127.0.0.1:{port}"},
                {**valid, "Host": f"127.0.0.1:{port}/path"},
                {**valid, "Origin": "null"},
                {**valid, "Origin": "http://127.0.0.1:9"},
                {**valid, "Origin": f"http://127.0.0.1:{port}/path"},
            ]
            for headers in cases:
                connection = http.client.HTTPConnection("127.0.0.1", port)
                connection.request("POST", "/api/native-stop/prepare", "{}", headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                self.assertEqual(json.load(response)["code"], "control_request_rejected")
                connection.close()
            self.assertEqual(stopper.calls, [])
            connection = http.client.HTTPConnection("127.0.0.1", port)
            connection.request("POST", "/api/native-stop/prepare", '{"agentRef":"agent-1"}', valid)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response)["confirmationRef"], "opaque")
            self.assertEqual(stopper.calls, ["agent-1"])
            self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", port)
            connection.request("GET", "/api/native-stop/prepare")
            response = connection.getresponse()
            self.assertEqual(response.status, 405)
            self.assertEqual(json.load(response)["outcome"], "not_sent")
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
