from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import threading
from typing import Any, Callable, cast
import unittest
from unittest.mock import patch

from switchstand.app_server import CodexAppServer
from switchstand.legacy_adapter import CodexAdapter
from switchstand.legacy_deadline import (
    LegacyDeadline,
    PersistenceUnavailable,
    PhaseResult,
    SetupResult,
)
from switchstand.legacy_transport import (
    _notify_initialized,
    close_legacy,
    open_legacy,
    request_legacy,
)


class FakeSocket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.shutdowns = 0
        self.closes = 0
        self.connects = 0
        self.fail_settimeout = False
        self.on_settimeout: Callable[[], None] | None = None

    def settimeout(self, value: float) -> None:
        if self.fail_settimeout:
            raise OSError("settimeout failed")
        self.timeouts.append(value)
        if self.on_settimeout is not None:
            self.on_settimeout()

    def shutdown(self, _how: int) -> None:
        self.shutdowns += 1

    def connect(self, _path: str) -> None:
        self.connects += 1

    def close(self) -> None:
        self.closes += 1


class FakeReader:
    def __init__(self) -> None:
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


def client_with(replies: list[object], *, send_error: BaseException | None = None):
    client = object.__new__(CodexAppServer)
    fake = cast(Any, client)
    fake._lock = threading.Lock()
    fake._next_id = 0
    fake._server_messages = deque()
    fake.socket = FakeSocket()
    fake.reader = FakeReader()
    sent: list[tuple[int, bytes]] = []

    def send(opcode: int, payload: bytes) -> None:
        sent.append((opcode, payload))
        if send_error is not None:
            raise send_error

    fake._send_frame = send
    values = iter(replies)
    fake._read_text = lambda *_args, **_kwargs: str(next(values))
    return fake, sent


class LegacyTransportTests(unittest.TestCase):
    def test_request_outcomes_are_closed_and_exact(self) -> None:
        cases = (
            ({"id": 1, "result": {"exact": True}}, "acknowledged"),
            ({"id": 1, "error": {"code": 1, "message": "rejected"}}, "rejected"),
            ({"id": 1, "result": {}, "error": {}}, "ambiguous"),
            ({"id": 1, "result": []}, "ambiguous"),
            ({"id": 1, "error": None}, "ambiguous"),
            ({"id": 1, "error": "not-an-error-object"}, "ambiguous"),
            ({"id": 1, "error": {"code": True, "message": "bad"}}, "ambiguous"),
            ({"id": 1, "error": {"code": 1}}, "ambiguous"),
            ({"id": True, "result": {"exact": True}}, "ambiguous"),
            ({"id": 1.0, "result": {"exact": True}}, "ambiguous"),
            ({"id": 2, "result": {"exact": True}}, "ambiguous"),
            ("not json", "ambiguous"),
        )
        for reply, expected in cases:
            with self.subTest(expected=expected, reply=reply):
                raw = reply if isinstance(reply, str) else json.dumps(reply)
                client, sent = client_with([raw])
                result = request_legacy(client, "target", {}, LegacyDeadline.after(1))
                self.assertEqual(result.disposition, expected)
                self.assertEqual(len(sent), 1)

        client, sent = client_with([], send_error=OSError("partial"))
        result = request_legacy(client, "target", {}, LegacyDeadline.after(1))
        self.assertEqual(result.disposition, "ambiguous")
        self.assertEqual(len(sent), 1)

    def test_parser_limits_constants_and_exact_envelopes_are_ambiguous(self) -> None:
        deep = "[" * 101 + "0" + "]" * 101
        cases = (
            '{"id":1,"result":{"number":' + "9" * 5000 + "}}",
            '{"id":1,"result":{"deep":' + deep + "}}",
            '{"id":1,"result":{"number":NaN}}',
            '{"id":1,"result":{"number":Infinity}}',
            '{"id":1,"result":{"number":-Infinity}}',
            '{"id":1,"result":{"number":1e400}}',
            '{"id":1,"result":{},"extra":true}',
            '{"id":1,"error":{"code":1,"message":"bad"},"extra":true}',
            '{"id":1,"id":1,"result":{}}',
        )
        for raw in cases:
            with self.subTest(raw=raw[:80]):
                client, sent = client_with([raw])
                result = request_legacy(client, "target", {}, LegacyDeadline.after(1))
                self.assertEqual((result.disposition, result.code), ("ambiguous", "malformed_response"))
                self.assertEqual(len(sent), 1)

    def test_only_exact_server_messages_are_ignored_while_waiting(self) -> None:
        malformed = (
            '{"method":"status","result":{}}',
            '{"method":"status","params":[],"result":{}}',
            '{"method":"","params":{}}',
            '{"method":"status","params":{},"extra":true}',
        )
        acknowledgement = '{"id":1,"result":{"exact":true}}'
        for raw in malformed:
            with self.subTest(raw=raw):
                client, _sent = client_with([raw, acknowledgement])
                result = request_legacy(client, "target", {}, LegacyDeadline.after(1))
                self.assertEqual((result.disposition, result.code), ("ambiguous", "malformed_response"))
                self.assertEqual(list(client._server_messages), [])

        notification = '{"method":"status","params":{"type":"idle"}}'
        client, _sent = client_with([notification, acknowledgement])
        result = request_legacy(client, "target", {}, LegacyDeadline.after(1))
        self.assertEqual(result.disposition, "acknowledged")
        self.assertEqual(list(client._server_messages), [{"method": "status", "params": {"type": "idle"}}])

    def test_post_send_parser_failure_is_ambiguous_and_adapter_forces_cleanup(self) -> None:
        client, _sent = client_with([
            '{"id":1,"result":{"thread":{"id":"thread-1"}}}',
            '{"id":2,"result":{"number":' + "9" * 5000 + "}}",
        ])
        adapter = CodexAdapter(Path("/private/socket"), cwd=Path("/workspace"))
        setup = SetupResult(client, PhaseResult("acknowledged", "setup", {}), "sent")
        with patch("switchstand.legacy_adapter.open_legacy", return_value=setup):
            receipt = adapter.start_message_bounded(
                thread_id="thread-1",
                message={"id": "message-1", "text": "text"},
                deadline=LegacyDeadline.after(1),
            )
        self.assertEqual((receipt.phase.disposition, receipt.phase.code), ("ambiguous", "malformed_response"))
        self.assertEqual(client.reader.closes, 1)
        self.assertEqual(client.socket.shutdowns, 1)
        self.assertEqual(client.socket.closes, 1)

    def test_expired_request_and_notification_are_not_sent(self) -> None:
        client, sent = client_with([])
        expired = LegacyDeadline(0.0, lambda: 1.0)
        self.assertEqual(request_legacy(client, "target", {}, expired).disposition, "not_sent")
        self.assertEqual(_notify_initialized(client, expired), "not_sent")
        self.assertEqual(sent, [])

    def test_cutoff_during_timeout_setup_admits_no_request_notification_or_close_frame(self) -> None:
        for phase in ("request", "initialized", "close"):
            with self.subTest(phase=phase):
                now = [0.0]
                client, sent = client_with([])
                client.socket.on_settimeout = lambda now=now: now.__setitem__(0, 2.0)
                deadline = LegacyDeadline(1.0, lambda now=now: now[0])
                if phase == "request":
                    disposition = request_legacy(client, "target", {}, deadline).disposition
                elif phase == "initialized":
                    disposition = _notify_initialized(client, deadline)
                else:
                    disposition = close_legacy(client, deadline)
                self.assertEqual(disposition, "not_sent")
                self.assertEqual(sent, [])

        now = [0.0]
        socket_value = FakeSocket()
        socket_value.on_settimeout = lambda: now.__setitem__(0, 2.0)
        with patch("switchstand.legacy_transport.socket.socket", return_value=socket_value):
            setup = open_legacy(Path("/private/socket"), LegacyDeadline(1.0, lambda: now[0]))
        self.assertEqual(setup.phase.disposition, "not_sent")
        self.assertEqual(socket_value.connects, 0)
        self.assertEqual(socket_value.closes, 1)

    def test_initialized_is_sent_only_after_full_sendall_or_ambiguous(self) -> None:
        client, sent = client_with([])
        self.assertEqual(_notify_initialized(client, LegacyDeadline.after(1)), "sent")
        self.assertEqual(len(sent), 1)
        client, sent = client_with([], send_error=OSError("partial"))
        self.assertEqual(_notify_initialized(client, LegacyDeadline.after(1)), "ambiguous")
        self.assertEqual(len(sent), 1)

    def test_expired_close_sends_no_frame_and_forces_both_descriptors_closed(self) -> None:
        client, sent = client_with([])
        disposition = close_legacy(client, LegacyDeadline(0.0, lambda: 1.0))
        self.assertEqual(disposition, "not_sent")
        self.assertEqual(sent, [])
        self.assertEqual(client.reader.closes, 1)
        self.assertEqual(client.socket.shutdowns, 1)
        self.assertEqual(client.socket.closes, 1)

    def test_close_contains_timeout_setup_failure_and_forces_cleanup(self) -> None:
        client, sent = client_with([])
        client.socket.fail_settimeout = True
        self.assertEqual(close_legacy(client, LegacyDeadline.after(1)), "ambiguous")
        self.assertEqual(sent, [])
        self.assertEqual(client.reader.closes, 1)
        self.assertEqual(client.socket.shutdowns, 1)
        self.assertEqual(client.socket.closes, 1)

    def test_close_disposition_never_downgrades_exact_target_phase(self) -> None:
        adapter = CodexAdapter(Path("/private/socket"), cwd=Path("/workspace"))
        for phase in (
            PhaseResult("acknowledged", "turn/start", {"turn": {"id": "turn-1"}}),
            PhaseResult("rejected", "turn/start", code="app_server_rejected"),
        ):
            for close in ("not_sent", "sent", "ambiguous"):
                with self.subTest(phase=phase.disposition, close=close):
                    client, _sent = client_with([])
                    setup = SetupResult(
                        client,
                        PhaseResult("acknowledged", "setup", {}),
                        "sent",
                    )
                    with (
                        patch("switchstand.legacy_adapter.open_legacy", return_value=setup),
                        patch(
                            "switchstand.legacy_adapter.request_legacy",
                            side_effect=[
                                PhaseResult(
                                    "acknowledged",
                                    "thread/resume",
                                    {"thread": {"id": "thread-1"}},
                                ),
                                phase,
                            ],
                        ),
                        patch("switchstand.legacy_adapter.close_legacy", return_value=close),
                    ):
                        receipt = adapter.start_message_bounded(
                            thread_id="thread-1",
                            message={"id": "message-1", "text": "text"},
                            deadline=LegacyDeadline.after(1),
                        )
                    self.assertEqual(receipt.phase, phase)
                    self.assertEqual(receipt.close_disposition, close)

    def test_close_failure_cannot_replace_persistence_callback_failure(self) -> None:
        adapter = CodexAdapter(Path("/private/socket"), cwd=Path("/workspace"))
        client, _sent = client_with([])
        client.socket.fail_settimeout = True
        setup = SetupResult(client, PhaseResult("acknowledged", "setup", {}), "sent")

        def fail_persistence(_thread_id: str) -> None:
            raise PersistenceUnavailable("legacy persistence is unavailable")

        with (
            patch("switchstand.legacy_adapter.open_legacy", return_value=setup),
            patch(
                "switchstand.legacy_adapter.request_legacy",
                return_value=PhaseResult(
                    "acknowledged", "thread/start", {"thread": {"id": "thread-1"}}
                ),
            ),
            self.assertRaises(PersistenceUnavailable),
        ):
            adapter.create_attempt_bounded(
                role={"name": "Role A"},
                context={},
                deadline=LegacyDeadline.after(1),
                on_thread_id=fail_persistence,
            )
        self.assertEqual(client.reader.closes, 1)
        self.assertEqual(client.socket.closes, 1)

    def test_numeric_thread_identity_is_never_an_exact_acknowledgement(self) -> None:
        adapter = CodexAdapter(Path("/private/socket"), cwd=Path("/workspace"))
        client, _sent = client_with([])
        setup = SetupResult(client, PhaseResult("acknowledged", "setup", {}), "sent")
        observed: list[str] = []
        with (
            patch("switchstand.legacy_adapter.open_legacy", return_value=setup),
            patch(
                "switchstand.legacy_adapter.request_legacy",
                return_value=PhaseResult(
                    "acknowledged", "thread/start", {"thread": {"id": 123}}
                ),
            ),
            patch("switchstand.legacy_adapter.close_legacy", return_value="sent"),
        ):
            receipt = adapter.create_attempt_bounded(
                role={"name": "Role A"},
                context={},
                deadline=LegacyDeadline.after(1),
                on_thread_id=observed.append,
            )
        self.assertEqual(receipt.phase.disposition, "ambiguous")
        self.assertEqual(receipt.phase.code, "missing_exact_acknowledgement")
        self.assertEqual(observed, [])

    def test_exact_thread_identity_arriving_at_cutoff_skips_optional_name_request(self) -> None:
        adapter = CodexAdapter(Path("/private/socket"), cwd=Path("/workspace"))
        client, _sent = client_with([])
        setup = SetupResult(client, PhaseResult("acknowledged", "setup", {}), "sent")
        now = [0.0]

        def close_thread_identity(thread_id: str) -> None:
            self.assertEqual(thread_id, "thread-1")
            now[0] = 6.0

        with (
            patch("switchstand.legacy_adapter.open_legacy", return_value=setup),
            patch(
                "switchstand.legacy_adapter.request_legacy",
                return_value=PhaseResult(
                    "acknowledged", "thread/start", {"thread": {"id": "thread-1"}}
                ),
            ) as request,
            patch("switchstand.legacy_adapter.close_legacy", return_value="not_sent"),
        ):
            receipt = adapter.create_attempt_bounded(
                role={"name": "Role A"},
                context={},
                deadline=LegacyDeadline(5.0, lambda: now[0]),
                on_thread_id=close_thread_identity,
            )
        self.assertEqual(receipt.phase.disposition, "acknowledged")
        self.assertEqual(receipt.name_disposition, "not_sent")
        self.assertEqual(receipt.close_disposition, "not_sent")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1], "thread/start")

    def test_exact_thread_identity_survives_every_name_and_close_outcome(self) -> None:
        adapter = CodexAdapter(Path("/private/socket"), cwd=Path("/workspace"))
        for name in ("not_sent", "rejected", "ambiguous", "acknowledged"):
            for close in ("not_sent", "sent", "ambiguous"):
                with self.subTest(name=name, close=close):
                    client, _sent = client_with([])
                    setup = SetupResult(client, PhaseResult("acknowledged", "setup", {}), "sent")
                    observed: list[str] = []
                    with (
                        patch("switchstand.legacy_adapter.open_legacy", return_value=setup),
                        patch(
                            "switchstand.legacy_adapter.request_legacy",
                            side_effect=[
                                PhaseResult(
                                    "acknowledged",
                                    "thread/start",
                                    {"thread": {"id": "thread-1"}},
                                ),
                                PhaseResult(cast(Any, name), "thread/name/set", {}),
                            ],
                        ),
                        patch("switchstand.legacy_adapter.close_legacy", return_value=close),
                    ):
                        receipt = adapter.create_attempt_bounded(
                            role={"name": "Role A"},
                            context={},
                            deadline=LegacyDeadline.after(1),
                            on_thread_id=observed.append,
                        )
                    self.assertEqual(receipt.phase.disposition, "acknowledged")
                    self.assertEqual(receipt.name_disposition, name)
                    self.assertEqual(receipt.close_disposition, close)
                    self.assertEqual(observed, ["thread-1"])

    def test_target_is_never_admitted_when_initialized_was_not_sent(self) -> None:
        adapter = CodexAdapter(Path("/private/socket"), cwd=Path("/workspace"))
        setup = SetupResult(
            None,
            PhaseResult("ambiguous", "initialized", code="setup_ambiguous"),
            "ambiguous",
        )
        with (
            patch("switchstand.legacy_adapter.open_legacy", return_value=setup),
            patch("switchstand.legacy_adapter.request_legacy") as request,
        ):
            receipt = adapter.start_message_bounded(
                thread_id="thread",
                message={"id": "message", "text": "text"},
                deadline=LegacyDeadline.after(1),
            )
        self.assertEqual(receipt.phase.code, "setup_ambiguous")
        request.assert_not_called()

    def test_target_is_never_admitted_after_malformed_resume_identity(self) -> None:
        adapter = CodexAdapter(Path("/private/socket"), cwd=Path("/workspace"))
        for resumed_id in (123, "other-thread", None):
            with self.subTest(resumed_id=resumed_id):
                client, _sent = client_with([])
                setup = SetupResult(client, PhaseResult("acknowledged", "setup", {}), "sent")
                responses = [
                    PhaseResult(
                        "acknowledged",
                        "thread/resume",
                        {"thread": {"id": resumed_id}},
                    )
                ]
                with (
                    patch("switchstand.legacy_adapter.open_legacy", return_value=setup),
                    patch(
                        "switchstand.legacy_adapter.request_legacy", side_effect=responses
                    ) as request,
                    patch("switchstand.legacy_adapter.close_legacy", return_value="sent"),
                ):
                    receipt = adapter.start_message_bounded(
                        thread_id="thread-1",
                        message={"id": "message-1", "text": "text"},
                        deadline=LegacyDeadline.after(1),
                    )
                self.assertEqual(receipt.phase.disposition, "ambiguous")
                self.assertEqual(receipt.phase.phase, "thread/resume")
                self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
