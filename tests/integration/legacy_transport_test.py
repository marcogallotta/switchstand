from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
from typing import Any
import unittest
from unittest.mock import patch

from switchstand.app_server import CodexAppServer
from switchstand.engine import CodexAdapter, Engine


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def receive_exact(connection: socket.socket, length: int) -> bytes:
    value = bytearray()
    while len(value) < length:
        chunk = connection.recv(length - len(value))
        if not chunk:
            raise EOFError
        value.extend(chunk)
    return bytes(value)


def receive_frame(connection: socket.socket) -> tuple[int, bytes]:
    first, second = receive_exact(connection, 2)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", receive_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", receive_exact(connection, 8))[0]
    mask = receive_exact(connection, 4) if second & 0x80 else b""
    payload = receive_exact(connection, length)
    if mask:
        payload = bytes(item ^ mask[index % 4] for index, item in enumerate(payload))
    return first & 0x0F, payload


def send_json(connection: socket.socket, value: object) -> None:
    payload = json.dumps(value, separators=(",", ":")).encode()
    length = len(payload)
    header = bytes((0x81, length)) if length < 126 else bytes((0x81, 126)) + struct.pack("!H", length)
    connection.sendall(header + payload)


def send_raw(connection: socket.socket, payload: bytes) -> None:
    length = len(payload)
    header = bytes((0x81, length)) if length < 126 else bytes((0x81, 126)) + struct.pack("!H", length)
    connection.sendall(header + payload)


class DeadlinePeer(threading.Thread):
    def __init__(self, path: Path, actions: tuple[str, ...]) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.actions = actions
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.methods: list[str] = []
        self.close_frames: list[bool] = []
        self.connections = 0

    def message(self, connection: socket.socket) -> dict[str, object]:
        opcode, payload = receive_frame(connection)
        if opcode != 1:
            raise AssertionError(f"expected text frame, got {opcode}")
        value = json.loads(payload)
        self.methods.append(str(value.get("method")))
        return value

    def websocket_upgrade(self, connection: socket.socket) -> None:
        request = bytearray()
        while b"\r\n\r\n" not in request:
            request.extend(connection.recv(4096))
        headers = {}
        for line in request.decode("ascii").split("\r\n")[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.lower()] = value.strip()
        accept = base64.b64encode(
            hashlib.sha1((headers["sec-websocket-key"] + GUID).encode()).digest()
        ).decode()
        connection.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )

    def setup(self, connection: socket.socket) -> None:
        self.websocket_upgrade(connection)
        initialize = self.message(connection)
        send_json(connection, {"id": initialize["id"], "result": {}})
        initialized = self.message(connection)
        if initialized != {"method": "initialized", "params": {}}:
            raise AssertionError("unexpected initialized notification")

    def close_observation(self, connection: socket.socket) -> None:
        try:
            opcode, _ = receive_frame(connection)
            self.close_frames.append(opcode == 8)
        except (EOFError, OSError, socket.timeout):
            self.close_frames.append(False)

    def execute(self, server: socket.socket, action: str) -> None:
        connection, _ = server.accept()
        self.connections += 1
        connection.settimeout(1)
        with connection:
            if action == "upgrade_stall":
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    request.extend(connection.recv(4096))
                time.sleep(0.12)
                return
            self.websocket_upgrade(connection)
            if action == "initialize_send_stall":
                time.sleep(0.12)
                return
            initialize = self.message(connection)
            if action == "initialize_stall":
                time.sleep(0.12)
                return
            send_json(connection, {"id": initialize["id"], "result": {}})
            if action == "initialized_send_stall":
                time.sleep(0.12)
                return
            self.message(connection)
            if action == "creation_send_stall":
                time.sleep(0.12)
                return
            request = self.message(connection)
            method = str(request["method"])
            if action == "read_stall":
                if method != "thread/read":
                    raise AssertionError(method)
                time.sleep(0.12)
            elif action == "creation_success":
                if method != "thread/start":
                    raise AssertionError(method)
                send_json(connection, {"id": request["id"], "result": {"thread": {"id": "thread-1"}}})
                name = self.message(connection)
                if name["method"] != "thread/name/set":
                    raise AssertionError(name)
                send_json(connection, {"id": name["id"], "result": {}})
            elif action == "name_stall":
                send_json(connection, {"id": request["id"], "result": {"thread": {"id": "thread-1"}}})
                if self.message(connection)["method"] != "thread/name/set":
                    raise AssertionError("name was not attempted")
                time.sleep(0.12)
            elif action == "name_send_stall":
                send_json(connection, {"id": request["id"], "result": {"thread": {"id": "thread-1"}}})
                time.sleep(0.12)
            elif action in {
                "target_stall",
                "target_success",
                "target_success_close_stall",
                "target_send_stall",
                "interrupt_stall",
                "resume_stall",
                "target_malformed",
                "target_decode",
                "target_oversize",
                "target_integer_limit",
                "target_depth_limit",
                "target_nonstandard_constant",
                "target_malformed_notification",
            }:
                if method != "thread/resume":
                    raise AssertionError(method)
                if action == "resume_stall":
                    time.sleep(0.12)
                    return
                send_json(connection, {"id": request["id"], "result": {"thread": {"id": "thread-1"}}})
                if action == "target_send_stall":
                    time.sleep(0.12)
                    return
                target = self.message(connection)
                expected = {
                    "interrupt_stall": "turn/interrupt",
                }.get(action, "turn/start")
                if target["method"] != expected:
                    raise AssertionError(target)
                if action in {"target_stall", "interrupt_stall"}:
                    time.sleep(0.12)
                elif action == "target_malformed":
                    send_raw(connection, b"not-json")
                elif action == "target_decode":
                    send_raw(connection, b"\xff")
                elif action == "target_oversize":
                    connection.sendall(bytes((0x81, 127)) + struct.pack("!Q", 16 * 1024 * 1024 + 1))
                elif action == "target_integer_limit":
                    payload = b'{"id":' + str(target["id"]).encode() + b',"result":{"n":' + b"9" * 5000 + b"}}"
                    send_raw(connection, payload)
                elif action == "target_depth_limit":
                    deep = b"[" * 101 + b"0" + b"]" * 101
                    payload = b'{"id":' + str(target["id"]).encode() + b',"result":{"x":' + deep + b"}}"
                    send_raw(connection, payload)
                elif action == "target_nonstandard_constant":
                    payload = b'{"id":' + str(target["id"]).encode() + b',"result":{"x":NaN}}'
                    send_raw(connection, payload)
                elif action == "target_malformed_notification":
                    send_raw(connection, b'{"method":"status","result":{}}')
                    send_json(connection, {"id": target["id"], "result": {"turn": {"id": "turn-1"}}})
                else:
                    send_json(connection, {"id": target["id"], "result": {"turn": {"id": "turn-1"}}})
                    if action == "target_success_close_stall":
                        time.sleep(0.12)
                        return
            else:
                raise AssertionError(action)
            self.close_observation(connection)

    def run(self) -> None:
        server: socket.socket | None = None
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.path))
            server.listen(len(self.actions))
            server.settimeout(2)
            self.ready.set()
            for action in self.actions:
                self.execute(server, action)
        except BaseException as exc:
            self.error = exc
            self.ready.set()
        finally:
            if server is not None:
                server.close()


def attempt(state: dict[str, Any]) -> dict[str, Any]:
    roles = state["roles"]
    assert isinstance(roles, dict)
    attempt_id = roles["role-a"]["current_attempt_id"]
    attempts = state["attempts"]
    assert isinstance(attempts, list)
    return next(item for item in attempts if item["id"] == attempt_id)


class LegacyTransportIntegrationTests(unittest.TestCase):
    def run_case(
        self,
        actions: tuple[str, ...],
        *,
        stalled_method: str | None = None,
        stalled_occurrence: int = 1,
    ) -> tuple[Engine, DeadlinePeer, dict[str, Any]]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        path = root / "app-server.sock"
        peer = DeadlinePeer(path, actions)
        peer.start()
        self.assertTrue(peer.ready.wait(1))
        if peer.error:
            raise peer.error
        engine = Engine(
            root / "state.json",
            CodexAdapter(path, cwd=root),
            operation_deadline_seconds=0.05,
        )
        if stalled_method is None:
            state = engine.enqueue_snapshot("role-a", "bounded")
        else:
            original_send = CodexAppServer._send_frame
            matching_sends = 0

            def send_or_stall(client: CodexAppServer, opcode: int, payload: bytes) -> None:
                nonlocal matching_sends
                method = "close" if opcode == 8 else str(json.loads(payload).get("method") or "")
                if method == stalled_method:
                    matching_sends += 1
                    if matching_sends == stalled_occurrence:
                        client.socket.sendall(b"x" * (32 * 1024 * 1024))
                        return
                original_send(client, opcode, payload)

            with patch.object(CodexAppServer, "_send_frame", send_or_stall):
                state = engine.enqueue_snapshot("role-a", "bounded")
        peer.join(2)
        self.assertFalse(peer.is_alive())
        if peer.error:
            raise peer.error
        return engine, peer, state

    def run_followup(self, action: str, operation: str) -> tuple[Engine, DeadlinePeer, dict[str, Any]]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        path = root / "app-server.sock"
        peer = DeadlinePeer(path, ("creation_success", "target_success", action))
        peer.start()
        self.assertTrue(peer.ready.wait(1))
        if peer.error:
            raise peer.error
        engine = Engine(
            root / "state.json",
            CodexAdapter(path, cwd=root),
            operation_deadline_seconds=0.05,
        )
        state = engine.enqueue_snapshot("role-a", "bounded")
        record = attempt(state)
        if operation == "interrupt":
            state = engine.stop_snapshot(record["id"])
        elif operation == "read":
            state = engine.reconcile_snapshot()
        else:
            raise AssertionError(operation)
        peer.join(2)
        self.assertFalse(peer.is_alive())
        if peer.error:
            raise peer.error
        return engine, peer, state

    def test_upgrade_and_initialize_stalls_are_bounded_without_target_or_reconnect(self) -> None:
        for action in ("upgrade_stall", "initialize_stall"):
            with self.subTest(action=action):
                started = time.monotonic()
                _engine, peer, state = self.run_case((action,))
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(peer.connections, 1)
                self.assertNotIn("thread/start", peer.methods)
                self.assertEqual(attempt(state)["status"], "unknown")
                self.assertEqual(state["messages"][0]["status"], "queued")

    def test_real_backlogged_connect_is_bounded_without_retry(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        path = root / "backlogged.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        blocker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(path))
            server.listen(0)
            blocker.settimeout(1)
            blocker.connect(str(path))
            started = time.monotonic()
            engine = Engine(
                root / "state.json",
                CodexAdapter(path, cwd=root),
                operation_deadline_seconds=0.05,
            )
            state = engine.enqueue_snapshot("role-a", "bounded")
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(attempt(state)["status"], "unknown")
            self.assertEqual(len(state["attempts"]), 1)
        finally:
            blocker.close()
            server.close()

    def test_real_send_stalls_preserve_exact_prefix_and_never_reconnect(self) -> None:
        cases = (
            ("initialize_send_stall", "initialize", ("unknown", None), 1),
            ("initialized_send_stall", "initialized", ("unknown", None), 1),
            ("creation_send_stall", "thread/start", ("unknown", None), 1),
            ("name_send_stall", "thread/name/set", ("waiting", "thread-1"), 1),
            ("target_send_stall", "turn/start", ("unknown", "thread-1"), 2),
            ("target_success_close_stall", "close", ("running", "thread-1"), 2),
        )
        for action, method, expected, connections in cases:
            with self.subTest(action=action):
                actions = (action,) if connections == 1 else ("creation_success", action)
                started = time.monotonic()
                occurrence = 2 if action == "target_success_close_stall" else 1
                _engine, peer, state = self.run_case(
                    actions, stalled_method=method, stalled_occurrence=occurrence
                )
                self.assertLess(time.monotonic() - started, 0.7)
                record = attempt(state)
                self.assertEqual((record["status"], record["thread_id"]), expected)
                self.assertEqual(peer.connections, connections)
                self.assertLessEqual(peer.methods.count(method), 1)

    def test_resume_malformed_oversize_and_decode_are_ambiguous_single_shot(self) -> None:
        for action, expected_error in (
            ("resume_stall", "setup_ambiguous"),
            ("target_malformed", "turn_start_ambiguous"),
            ("target_decode", "turn_start_ambiguous"),
            ("target_oversize", "turn_start_ambiguous"),
        ):
            with self.subTest(action=action):
                _engine, peer, state = self.run_case(("creation_success", action))
                record = attempt(state)
                self.assertEqual(record["status"], "unknown")
                self.assertEqual(record["error"], expected_error)
                self.assertEqual(state["messages"][0]["status"], "unknown")
                self.assertEqual(peer.connections, 2)
                self.assertEqual(peer.methods.count("thread/resume"), 1)

    def test_parser_limits_and_malformed_notifications_are_ambiguous_and_closed(self) -> None:
        for action in (
            "target_integer_limit",
            "target_depth_limit",
            "target_nonstandard_constant",
            "target_malformed_notification",
        ):
            with self.subTest(action=action):
                _engine, peer, state = self.run_case(("creation_success", action))
                record = attempt(state)
                self.assertEqual((record["status"], record["error"]), ("unknown", "turn_start_ambiguous"))
                self.assertEqual(state["messages"][0]["status"], "unknown")
                self.assertEqual(peer.connections, 2)
                self.assertEqual(peer.methods.count("turn/start"), 1)
                self.assertEqual(peer.close_frames, [True, True])

    def test_exact_thread_id_survives_name_stall_without_dispatch_or_reconnect(self) -> None:
        _engine, peer, state = self.run_case(("name_stall",))
        record = attempt(state)
        self.assertEqual(record["thread_id"], "thread-1")
        self.assertEqual(record["status"], "waiting")
        self.assertEqual(record["error"], "thread_name_ambiguous")
        self.assertEqual(peer.methods.count("thread/start"), 1)
        self.assertEqual(peer.methods.count("thread/name/set"), 1)
        self.assertNotIn("turn/start", peer.methods)
        self.assertEqual(peer.connections, 1)
        self.assertEqual(peer.close_frames, [False])

    def test_target_response_stall_preserves_prefix_and_never_retries(self) -> None:
        _engine, peer, state = self.run_case(("creation_success", "target_stall"))
        record = attempt(state)
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(record["error"], "turn_start_ambiguous")
        self.assertEqual(state["messages"][0]["status"], "unknown")
        self.assertEqual(peer.methods.count("thread/start"), 1)
        self.assertEqual(peer.methods.count("turn/start"), 1)
        self.assertEqual(peer.connections, 2)
        self.assertEqual(peer.close_frames, [True, False])

    def test_interrupt_and_read_stalls_are_single_shot_and_conservative(self) -> None:
        for action, operation, expected_error in (
            ("interrupt_stall", "interrupt", "interrupt_ambiguous"),
            ("read_stall", "read", "observation_ambiguous"),
        ):
            with self.subTest(action=action):
                started = time.monotonic()
                _engine, peer, state = self.run_followup(action, operation)
                self.assertLess(time.monotonic() - started, 0.6)
                record = attempt(state)
                self.assertEqual(record["status"], "unknown")
                self.assertEqual(record["error"], expected_error)
                expected_method = "turn/interrupt" if operation == "interrupt" else "thread/read"
                self.assertEqual(peer.methods.count(expected_method), 1)
                self.assertEqual(peer.connections, 3)
                self.assertEqual(peer.close_frames, [True, True, False])

    def test_peer_that_never_acknowledges_close_cannot_delay_accepted_target(self) -> None:
        started = time.monotonic()
        _engine, peer, state = self.run_case(("creation_success", "target_success_close_stall"))
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertEqual(attempt(state)["status"], "running")
        self.assertEqual(peer.connections, 2)
        self.assertEqual(peer.close_frames, [True])

    def test_ordinary_success_preserves_rpc_sequence_and_exact_results(self) -> None:
        _engine, peer, state = self.run_case(("creation_success", "target_success"))
        record = attempt(state)
        self.assertEqual((record["thread_id"], record["turn_id"], record["status"]),
                         ("thread-1", "turn-1", "running"))
        self.assertEqual(state["messages"][0]["status"], "delivered")
        self.assertEqual(peer.close_frames, [True, True])
        self.assertEqual(peer.methods, [
            "initialize", "initialized", "thread/start", "thread/name/set",
            "initialize", "initialized", "thread/resume", "turn/start",
        ])


if __name__ == "__main__":
    unittest.main()
