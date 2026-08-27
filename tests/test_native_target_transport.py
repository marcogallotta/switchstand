from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Sequence, cast
import unittest
from unittest.mock import patch

from switchstand.current_target import ExactCurrentTarget
from switchstand.native_board import NativeBoard
from switchstand.native_target_transport import NativeTargetTransport


class BoundBoard:
    def __init__(self, target: ExactCurrentTarget, *, available: bool = True) -> None:
        self.target = target
        self.available = available
        self.calls: list[object] = []

    def _with_current_native_target(
        self,
        target: object,
        operation: Callable[[str], Any],
    ) -> Any:
        self.calls.append(target)
        if not self.available or target != self.target:
            return None
        return operation("PRIVATE-RAW-THREAD")


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.read_response: Any = {
            "thread": {
                "id": "PRIVATE-RAW-THREAD",
                "status": {"type": "idle"},
                "turns": [],
            }
        }

    def bounded_thread_read(self, thread_id: str, **limits: Any):
        self.calls.append(("read", thread_id, limits))
        return ("ok", self.read_response)

    def bounded_turn_start_text_native(self, thread_id: str, text: str, **limits: Any):
        self.calls.append(("start", thread_id, text, limits))
        return ("ok", {"turn": {"id": "new-turn"}})

    def bounded_turn_steer_text(
        self,
        thread_id: str,
        expected_turn_id: str,
        text: str,
        **limits: Any,
    ):
        self.calls.append(("steer", thread_id, expected_turn_id, text, limits))
        return ("ok", {"turnId": expected_turn_id})


class ClientFactory:
    def __init__(self, clients: Sequence[Client | BaseException]) -> None:
        self.clients = iter(clients)
        self.calls: list[tuple[object, dict[str, Any]]] = []

    def __call__(self, socket_path: object, **kwargs: Any) -> Client:
        self.calls.append((socket_path, kwargs))
        client = next(self.clients)
        if isinstance(client, BaseException):
            raise client
        return client


class NativeTargetTransportTests(unittest.TestCase):
    def transport(self, board: BoundBoard) -> NativeTargetTransport:
        return NativeTargetTransport(cast(NativeBoard, board), Path("PRIVATE-SOCKET"))

    def test_exact_methods_bind_inside_transport_and_preserve_limits(self):
        target = ExactCurrentTarget()
        board = BoundBoard(target)
        clients = [Client(), Client(), Client()]
        factory = ClientFactory(clients)
        transport = self.transport(board)

        with patch("switchstand.native_target_transport.CodexAppServer", factory):
            read_classification, read_response = transport.thread_read(
                target, max_response_bytes=1234, timeout_seconds=1.25
            )
            start = transport.turn_start(
                target,
                "  exact input\n",
                max_response_bytes=1234,
                timeout_seconds=1.25,
            )
            steer = transport.turn_steer(
                target,
                "active-turn",
                "correction",
                max_response_bytes=1234,
                timeout_seconds=1.25,
            )

        self.assertEqual(read_classification, "ok")
        self.assertIsNotNone(read_response)
        read_response = cast(dict[str, Any], read_response)
        self.assertIs(read_response["thread"]["id"], target)
        self.assertNotIn("PRIVATE-RAW-THREAD", repr(read_response))
        self.assertEqual(start, ("ok", {"turn": {"id": "new-turn"}}))
        self.assertEqual(steer, ("ok", {"turnId": "active-turn"}))
        self.assertEqual(board.calls, [target, target, target])
        self.assertEqual([client.calls[0][0] for client in clients], ["read", "start", "steer"])
        self.assertEqual(clients[0].calls[0], (
            "read", "PRIVATE-RAW-THREAD",
            {"max_response_bytes": 1234, "timeout_seconds": 1.25},
        ))
        self.assertEqual(clients[1].calls[0][1:3], (
            "PRIVATE-RAW-THREAD", "  exact input\n"
        ))
        self.assertEqual(clients[2].calls[0][1:4], (
            "PRIVATE-RAW-THREAD", "active-turn", "correction"
        ))
        self.assertTrue(all(call[1] == {
            "client_name": "switchstand-native-input",
            "timeout_seconds": 1.25,
            "bounded_stop": True,
            "bounded_response_bytes": 1234,
        } for call in factory.calls))

    def test_unavailable_or_changed_target_never_constructs_a_client(self):
        target = ExactCurrentTarget()
        board = BoundBoard(target, available=False)
        factory = ClientFactory([])
        transport = self.transport(board)

        with patch("switchstand.native_target_transport.CodexAppServer", factory):
            unavailable = transport.thread_read(
                target, max_response_bytes=100, timeout_seconds=1
            )
            changed = transport.turn_start(
                ExactCurrentTarget(), "input", max_response_bytes=100, timeout_seconds=1
            )
            invalid = transport.turn_steer(
                object(), "turn", "input", max_response_bytes=100, timeout_seconds=1
            )

        self.assertEqual(unavailable, ("unavailable", None))
        self.assertEqual(changed, ("unavailable", None))
        self.assertEqual(invalid, ("unavailable", None))
        self.assertEqual(factory.calls, [])

    def test_mismatched_read_and_setup_failure_are_safe_and_single_shot(self):
        target = ExactCurrentTarget()
        board = BoundBoard(target)
        mismatch = Client()
        mismatch.read_response["thread"]["id"] = "DIFFERENT-PRIVATE-THREAD"
        factory = ClientFactory([mismatch, RuntimeError("PRIVATE-SETUP-FAILURE")])
        transport = self.transport(board)

        with patch("switchstand.native_target_transport.CodexAppServer", factory):
            malformed = transport.thread_read(
                target, max_response_bytes=100, timeout_seconds=1
            )
            ambiguous = transport.turn_start(
                target, "input", max_response_bytes=100, timeout_seconds=1
            )

        self.assertEqual(malformed, ("malformed", None))
        self.assertEqual(ambiguous, ("ambiguous", None))
        disclosure = repr((malformed, ambiguous, vars(transport)))
        self.assertNotIn("DIFFERENT-PRIVATE-THREAD", disclosure)
        self.assertNotIn("PRIVATE-SETUP-FAILURE", disclosure)
        self.assertEqual(len(factory.calls), 2)


if __name__ == "__main__":
    unittest.main()
