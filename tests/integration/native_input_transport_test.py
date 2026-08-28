from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import socket
import tempfile
import time
from typing import Any, Mapping, cast
import unittest

from app_server_transport_test import ScriptedPeer, receive_frame, send_frame, send_json
from switchstand.app_server import CodexAppServer
from switchstand.current_target import ExactCurrentTarget
from switchstand.native_board import NativeBoard
from switchstand.native_input import NativeInput
from switchstand.native_target_transport import NativeTargetTransport


NOT_SENT = {"code": "input_unavailable", "outcome": "not_sent"}


@dataclass(frozen=True, repr=False)
class OpaqueTarget:
    token: str


class InputPeer(ScriptedPeer):
    def __init__(self, path: Path, plans: tuple[Mapping[str, Any], ...]) -> None:
        super().__init__(path)
        self.plans = plans

    def run(self) -> None:
        server = None
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.path))
            server.listen(max(1, len(self.plans)))
            server.settimeout(5)
            self.ready.set()
            for plan in self.plans:
                with self.upgrade(server, initialize_client=False) as connection:
                    initialize = self.message(connection)
                    self.assert_equal(initialize["method"], "initialize")
                    delay_initialize = plan.get("delay_initialize")
                    if isinstance(delay_initialize, (int, float)):
                        time.sleep(delay_initialize)
                    if plan.get("oversize_initialize"):
                        try:
                            send_json(connection, {
                                "id": initialize["id"], "result": {"padding": "x" * 8192}})
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                        self._receive_close(connection)
                        continue
                    try:
                        send_json(connection, {"id": initialize["id"], "result": {}})
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    if delay_initialize:
                        self._receive_close(connection)
                        continue
                    self.assert_equal(
                        self.message(connection), {"method": "initialized", "params": {}}
                    )
                    request = self.message(connection)
                    self.assert_equal(request["method"], plan["method"])
                    delay = plan.get("delay")
                    if isinstance(delay, (int, float)):
                        time.sleep(delay)
                    response = plan.get("response")
                    try:
                        if response == "malformed":
                            send_frame(connection, b'{"broken":')
                        elif response == "oversize":
                            send_frame(connection, b"x" * 8192)
                        elif "error" in plan:
                            send_json(connection, {"id": request["id"], "error": plan["error"]})
                        else:
                            send_json(connection, {"id": request["id"], "result": response})
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    self._receive_close(connection)
        except BaseException as exc:
            self.error = exc
            self.ready.set()
        finally:
            if server is not None:
                server.close()

    @staticmethod
    def _receive_close(connection: socket.socket) -> None:
        try:
            receive_frame(connection)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            pass


class BoundTransport:
    """Test-only opaque-handle binding to one scripted native thread id."""

    def __init__(self, socket_path: Path, target: OpaqueTarget) -> None:
        self.socket_path = socket_path
        self.target = target

    def _client(self, max_response_bytes: int, timeout_seconds: float) -> CodexAppServer:
        return CodexAppServer(
            self.socket_path,
            timeout_seconds=timeout_seconds,
            bounded_stop=True,
            bounded_response_bytes=max_response_bytes,
        )

    def turns_list(self, target, *, max_response_bytes, timeout_seconds):
        if target != self.target:
            return "malformed", None
        client = self._client(max_response_bytes, timeout_seconds)
        classification, response = client.stop_request(
            "thread/turns/list", {"threadId": "native-thread", "limit": 1,
                "sortDirection": "desc", "itemsView": "notLoaded"},
            max_response_bytes=max_response_bytes,
            timeout_seconds=timeout_seconds,
        )
        if classification != "ok" or response is None:
            return classification, None
        bound = deepcopy(dict(response))
        bound["target"] = target
        return "ok", bound

    def turn_start(self, target, text, *, max_response_bytes, timeout_seconds):
        if target != self.target:
            return "malformed", None
        return self._client(max_response_bytes, timeout_seconds).bounded_turn_start_text_native(
            "native-thread", text, max_response_bytes=max_response_bytes,
            timeout_seconds=timeout_seconds,
        )

    def turn_steer(
        self, target, expected_turn_id, text, *, max_response_bytes, timeout_seconds
    ):
        if target != self.target:
            return "malformed", None
        return self._client(max_response_bytes, timeout_seconds).bounded_turn_steer_text(
            "native-thread", expected_turn_id, text,
            max_response_bytes=max_response_bytes, timeout_seconds=timeout_seconds,
        )


class Resolver:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = 0

    def __call__(self, selection, *, now, maximum_observation_age_seconds):
        self.calls += 1
        return next(self.values)


class BindingBoard:
    """Minimal scripted owner of the production transport's private binding seam."""

    def __init__(self, target: ExactCurrentTarget, *, maximum_bindings: int = 99) -> None:
        self.target = target
        self.maximum_bindings = maximum_bindings
        self.calls = 0

    def _with_current_native_target(self, target, operation):
        self.calls += 1
        if self.calls > self.maximum_bindings or target != self.target:
            return None
        return operation("native-thread")


def input_request(text="  wire input\n"):
    return {
        "version": "native-input-v1",
        "observationRunRef": "opaque-run",
        "agentRef": "opaque-agent",
        "text": text,
    }


def read_response(status, turns):
    del status
    return {"data": [{**turn, "items": [], "itemsView": "notLoaded"}
        for turn in turns], "nextCursor": None}


class NativeInputTransportTest(unittest.TestCase):
    def run_case(
        self, plans, targets, *, timeout_seconds=1.0, max_response_bytes=4096
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "PRIVATE-SOCKET-SENTINEL.sock"
            peer = InputPeer(path, plans)
            peer.start()
            self.assertTrue(peer.ready.wait(5))
            target = next(value for value in targets if isinstance(value, OpaqueTarget))
            resolver = Resolver(targets)
            native_input = NativeInput(
                resolver,
                BoundTransport(path, target),
                maximum_observation_age_seconds=3,
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
            )
            result = native_input.send(input_request())
            peer.join(5)
            self.assertFalse(peer.is_alive())
            if peer.error:
                raise peer.error
            return result, resolver.calls, peer.requests

    def test_idle_start_and_active_exact_steer_wire_sequences(self):
        target = OpaqueTarget("stable")
        cases = (
            (
                (
                    {"method": "thread/turns/list", "response": read_response("idle", [])},
                    {"method": "turn/start", "response": {"turn": {"id": "new-turn"}}},
                ),
                "start",
                {"threadId": "native-thread", "input": [
                    {"type": "text", "text": "  wire input\n"}]},
            ),
            (
                (
                    {"method": "thread/turns/list", "response": read_response("active", [
                        {"id": "active-turn", "status": "inProgress"}])},
                    {"method": "turn/steer", "response": {"turnId": "active-turn"}},
                ),
                "steer",
                {"threadId": "native-thread", "input": [
                    {"type": "text", "text": "  wire input\n"}],
                    "expectedTurnId": "active-turn"},
            ),
        )
        for plans, mode, action_params in cases:
            with self.subTest(mode=mode):
                result, calls, requests = self.run_case(plans, [target, target])
                self.assertEqual(result, {
                    "code": "input_sent", "outcome": "sent", "mode": mode})
                self.assertEqual(calls, 2)
                methods = [item["method"] for item in requests]
                self.assertEqual(methods, [
                    "initialize", "initialized", "thread/turns/list",
                    "initialize", "initialized", f"turn/{mode}"])
                self.assertEqual(requests[-1]["params"], action_params)
                self.assertNotIn("approvalPolicy", repr(requests[-1]))
                self.assertNotIn("sandboxPolicy", repr(requests[-1]))

    def test_target_drift_sends_no_action_or_retry(self):
        first = OpaqueTarget("first")
        plans = ({"method": "thread/turns/list", "response": read_response("idle", [])},)
        result, calls, requests = self.run_case(plans, [first, OpaqueTarget("second")])
        self.assertEqual(result, NOT_SENT)
        self.assertEqual(calls, 2)
        self.assertEqual([item["method"] for item in requests], [
            "initialize", "initialized", "thread/turns/list"])

    def test_native_rejection_or_ack_race_never_switches_mode(self):
        target = OpaqueTarget("stable")
        plans = (
            {"method": "thread/turns/list", "response": read_response("idle", [])},
            {"method": "turn/start", "error": {"code": -1, "message": "race"}},
        )
        result, _, requests = self.run_case(plans, [target, target])
        self.assertEqual(result, NOT_SENT)
        methods = [item["method"] for item in requests]
        self.assertEqual(methods.count("turn/start"), 1)
        self.assertNotIn("turn/steer", methods)

    def test_malformed_oversize_timeout_and_setup_failures_are_safe_and_single_shot(self):
        target = OpaqueTarget("stable")
        cases = (
            ({"method": "thread/turns/list", "response": "malformed"}, 1.0, 4096),
            ({"method": "thread/turns/list", "response": "oversize"}, 1.0, 512),
            ({"method": "thread/turns/list", "response": read_response("idle", []),
                "delay": 0.08}, 0.03, 4096),
            ({"method": "thread/turns/list", "response": read_response("idle", []),
                "delay_initialize": 0.08}, 0.03, 4096),
            ({"method": "thread/turns/list", "response": read_response("idle", []),
                "oversize_initialize": True}, 1.0, 512),
        )
        for plan, timeout_seconds, max_response_bytes in cases:
            with self.subTest(plan=tuple(plan)):
                result, calls, requests = self.run_case(
                    (plan,), [target], timeout_seconds=timeout_seconds,
                    max_response_bytes=max_response_bytes,
                )
                self.assertEqual(result, NOT_SENT)
                self.assertEqual(calls, 1)
                self.assertLessEqual(
                    sum(item["method"] == "thread/turns/list" for item in requests), 1)
                self.assertFalse(any(
                    isinstance(item["method"], str) and item["method"].startswith("turn/")
                    for item in requests
                ))

    def run_production_case(self, plans, *, maximum_bindings=99):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "PRIVATE-SOCKET-SENTINEL.sock"
            peer = InputPeer(path, plans)
            peer.start()
            self.assertTrue(peer.ready.wait(5))
            target = ExactCurrentTarget()
            board = BindingBoard(target, maximum_bindings=maximum_bindings)
            native_input = NativeInput(
                Resolver([target, target]),
                NativeTargetTransport(cast(NativeBoard, board), path),
                maximum_observation_age_seconds=3,
                timeout_seconds=1,
                max_response_bytes=4096,
            )
            result = native_input.send(input_request())
            peer.join(5)
            self.assertFalse(peer.is_alive())
            if peer.error:
                raise peer.error
            return result, board.calls, peer.requests

    def test_production_transport_binds_opaque_target_on_scripted_unix_socket(self):
        result, calls, requests = self.run_production_case((
            {"method": "thread/turns/list", "response": read_response("idle", [])},
            {"method": "turn/start", "response": {"turn": {"id": "new-turn"}}},
        ))
        self.assertEqual(result, {
            "code": "input_sent", "outcome": "sent", "mode": "start"
        })
        self.assertEqual(calls, 2)
        self.assertEqual([request["method"] for request in requests], [
            "initialize", "initialized", "thread/turns/list",
            "initialize", "initialized", "turn/start",
        ])

    def test_production_transport_disappearance_before_action_fails_closed(self):
        result, calls, requests = self.run_production_case((
            {"method": "thread/turns/list", "response": read_response("idle", [])},
        ), maximum_bindings=1)
        self.assertEqual(result, NOT_SENT)
        self.assertEqual(calls, 2)
        self.assertEqual([request["method"] for request in requests], [
            "initialize", "initialized", "thread/turns/list",
        ])


if __name__ == "__main__":
    unittest.main()
