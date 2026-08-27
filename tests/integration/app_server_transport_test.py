from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest

from switchstand.app_server import CodexAppServer
from switchstand.native_board import NativeBoard
from switchstand.native_stop import NativeStop


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def receive_exact(connection: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = connection.recv(length - len(chunks))
        if not chunk:
            raise AssertionError("peer closed before the frame completed")
        chunks.extend(chunk)
    return bytes(chunks)


def receive_frame(connection: socket.socket) -> tuple[int, bytes, bool]:
    first, second = receive_exact(connection, 2)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", receive_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", receive_exact(connection, 8))[0]
    masked = bool(second & 0x80)
    mask = receive_exact(connection, 4) if masked else b""
    payload = receive_exact(connection, length)
    if masked:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return first & 0x0F, payload, masked


def send_frame(connection: socket.socket, payload: bytes, *, opcode: int = 1, final: bool = True) -> None:
    if len(payload) < 126:
        header = bytes(((0x80 if final else 0) | opcode, len(payload)))
    elif len(payload) <= 0xFFFF:
        header = bytes(((0x80 if final else 0) | opcode, 126)) + struct.pack("!H", len(payload))
    else:
        header = bytes(((0x80 if final else 0) | opcode, 127)) + struct.pack("!Q", len(payload))
    connection.sendall(header + payload)


def send_json(connection: socket.socket, value: object) -> None:
    send_frame(connection, json.dumps(value, separators=(",", ":")).encode())


class ScriptedPeer(threading.Thread):
    def __init__(self, path: Path, *, poll_statuses: tuple[str, ...] = ()) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.poll_statuses = poll_statuses
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.requests: list[dict[str, object]] = []
        self.masked: list[bool] = []
        self.connections = 0

    def message(self, connection: socket.socket) -> dict[str, object]:
        opcode, payload, masked = receive_frame(connection)
        self.masked.append(masked)
        self.assert_equal(opcode, 1)
        value = json.loads(payload)
        self.requests.append(value)
        return value

    @staticmethod
    def assert_equal(actual: object, expected: object) -> None:
        if actual != expected:
            raise AssertionError(f"expected {expected!r}, received {actual!r}")

    def upgrade(self, server: socket.socket, *, initialize_client: bool = True) -> socket.socket:
        connection, _ = server.accept()
        self.connections += 1
        connection.settimeout(5)
        request = bytearray()
        while b"\r\n\r\n" not in request:
            request.extend(connection.recv(4096))
        headers = {}
        for line in request.decode("ascii").split("\r\n")[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.lower()] = value.strip()
        key = headers["sec-websocket-key"]
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        connection.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )
        if initialize_client:
            initialize = self.message(connection)
            self.assert_equal(initialize["method"], "initialize")
            send_json(connection, {"id": initialize["id"], "result": {}})
            initialized = self.message(connection)
            self.assert_equal(initialized, {"method": "initialized", "params": {}})
        return connection

    @staticmethod
    def thread(thread_id: str, parent_id: str | None, status: str) -> dict[str, object]:
        native_status: dict[str, object] = {"type": status}
        if status == "active":
            native_status["activeFlags"] = []
        return {
            "id": thread_id,
            "sessionId": f"session-{thread_id}",
            "parentThreadId": parent_id,
            "source": "cli" if parent_id is None else {"subAgent": "review"},
            "createdAt": 100.0 if parent_id is None else 101.0,
            "updatedAt": 102.0,
            "status": native_status,
        }

    def poll_pass(self, connection: socket.socket, status: str) -> None:
        root = self.message(connection)
        self.assert_equal(root["method"], "thread/read")
        send_json(
            connection,
            {"id": root["id"], "result": {"thread": self.thread("root-1", None, status)}},
        )
        first_page = self.message(connection)
        self.assert_equal(first_page["method"], "thread/list")
        send_json(
            connection,
            {
                "id": first_page["id"],
                "result": {
                    "data": [self.thread("child-1", "root-1", status)],
                    "nextCursor": "page-2",
                },
            },
        )
        second_page = self.message(connection)
        self.assert_equal(second_page["method"], "thread/list")
        second_params = second_page.get("params")
        if not isinstance(second_params, dict):
            raise AssertionError("thread/list params are unavailable")
        self.assert_equal(second_params.get("cursor"), "page-2")
        send_json(
            connection,
            {"id": second_page["id"], "result": {"data": [], "nextCursor": None}},
        )
        opcode, _, masked = receive_frame(connection)
        self.masked.append(masked)
        self.assert_equal(opcode, 8)

    def run(self) -> None:
        server = None
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.path))
            server.listen(max(1, len(self.poll_statuses)))
            server.settimeout(5)
            self.ready.set()
            for status in self.poll_statuses:
                with self.upgrade(server) as connection:
                    self.poll_pass(connection, status)
            if not self.poll_statuses:
                with self.upgrade(server) as connection:
                    listing = self.message(connection)
                    self.assert_equal(listing["method"], "thread/list")
                    send_json(
                        connection,
                        {
                            "method": "thread/status/changed",
                            "params": {"threadId": "child-1", "status": {"type": "idle"}},
                        },
                    )
                    send_json(
                        connection,
                        {"id": listing["id"], "result": {"data": [], "nextCursor": None}},
                    )
                    opcode, _, masked = receive_frame(connection)
                    self.masked.append(masked)
                    self.assert_equal(opcode, 8)
        except BaseException as exc:
            self.error = exc
            self.ready.set()
        finally:
            if server is not None:
                server.close()


class StopPeer(ScriptedPeer):
    def run(self) -> None:
        server = None
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.path))
            server.listen(4)
            server.settimeout(5)
            self.ready.set()
            for status in ("inProgress", "inProgress", None, "interrupted"):
                with self.upgrade(server) as connection:
                    request = self.message(connection)
                    if status is None:
                        self.assert_equal(request["method"], "turn/interrupt")
                        self.assert_equal(request["params"], {"threadId": "root-1", "turnId": "turn-1"})
                        result = {}
                    else:
                        self.assert_equal(request["method"], "thread/read")
                        result = {"thread": {"id": "root-1", "status": {
                            "type": "active" if status == "inProgress" else "idle"},
                            "turns": [{"id": "turn-1", "status": status,
                                "items": [{"text": "PRIVATE-TRANSCRIPT-SENTINEL"}]}]}}
                    send_json(connection, {"id": request["id"], "result": result})
                    receive_frame(connection)
        except BaseException as exc:
            self.error = exc
            self.ready.set()
        finally:
            if server is not None:
                server.close()


class OversizePeer(ScriptedPeer):
    def run(self) -> None:
        server = None
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.path))
            server.listen(2)
            server.settimeout(5)
            self.ready.set()
            for fragmented in (False, True):
                with self.upgrade(server) as connection:
                    self.message(connection)
                    if fragmented:
                        send_frame(connection, b"x" * 40, final=False)
                        send_frame(connection, b"\xff" * 40, opcode=0)
                    else:
                        send_frame(connection, b"\xff" * 80)
                    receive_frame(connection)
        except BaseException as exc:
            self.error = exc
            self.ready.set()
        finally:
            if server is not None:
                server.close()


class SetupPeer(ScriptedPeer):
    def run(self) -> None:
        server = None
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.path))
            server.listen(3)
            server.settimeout(5)
            self.ready.set()
            with self.upgrade(server, initialize_client=False) as connection:
                initialize = self.message(connection)
                send_json(connection, {"method": "private", "params": {"text": "SETUP-SENTINEL"}})
                send_json(connection, {"id": initialize["id"], "result": {}})
                self.assert_equal(self.message(connection)["method"], "initialized")
                target = self.message(connection)
                send_json(connection, {"id": target["id"], "result": {}})
                receive_frame(connection)
            with self.upgrade(server, initialize_client=False) as connection:
                self.message(connection)
                send_frame(connection, b"x" * 140_000, final=False)
                send_frame(connection, b"\xff" * 140_000, opcode=0)
                receive_frame(connection)
            with self.upgrade(server, initialize_client=False) as connection:
                self.message(connection)
                send_frame(connection, b'{"SETUP-SENTINEL":')
                receive_frame(connection)
        except BaseException as exc:
            self.error = exc
            self.ready.set()
        finally:
            if server is not None:
                server.close()


class AppServerTransportTest(unittest.TestCase):
    def test_bounded_stop_setup_discards_sentinel_and_closes_oversize_or_malformed(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "setup.sock"
            peer = SetupPeer(socket_path)
            peer.start()
            self.assertTrue(peer.ready.wait(5))
            client = CodexAppServer(socket_path, timeout_seconds=3, bounded_stop=True)
            classification, result = client.stop_request("thread/read", {})
            self.assertEqual((classification, result), ("ok", {}))
            self.assertEqual(client.drain_server_messages(), [])
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "^setup_unavailable$") as raised:
                    CodexAppServer(socket_path, timeout_seconds=3, bounded_stop=True)
                self.assertNotIn("SETUP-SENTINEL", repr(raised.exception))
                self.assertIsNone(raised.exception.__context__)
            peer.join(5)
            if peer.error:
                raise peer.error
            self.assertEqual([request["method"] for request in peer.requests],
                ["initialize", "initialized", "thread/read", "initialize", "initialize"])

    def test_native_stop_real_unix_sequence_targets_once_and_confirms_exact_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "stop.sock"
            peer = StopPeer(socket_path)
            peer.start()
            self.assertTrue(peer.ready.wait(5))
            stop = NativeStop(lambda: CodexAppServer(socket_path, timeout_seconds=3,
                bounded_stop=True), lambda _: "root-1")
            prepared = stop.prepare("agent-1")
            requested = stop.commit(prepared["confirmationRef"])
            confirmed = stop.status(requested["operationRef"])
            peer.join(5)
            if peer.error:
                raise peer.error
            self.assertEqual((requested["outcome"], confirmed["outcome"]), ("requested", "confirmed"))
            methods = [request["method"] for request in peer.requests]
            self.assertEqual(methods.count("turn/interrupt"), 1)
            self.assertEqual(methods[2::3], ["thread/read", "thread/read", "turn/interrupt", "thread/read"])

    def test_stop_seam_rejects_complete_and_cumulative_oversize_before_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "oversize.sock"
            peer = OversizePeer(socket_path)
            peer.start()
            self.assertTrue(peer.ready.wait(5))
            outcomes = []
            for _ in range(2):
                client = CodexAppServer(socket_path, timeout_seconds=3, bounded_stop=True)
                outcomes.append(client.stop_request("thread/read", {}, max_response_bytes=64)[0])
            peer.join(5)
            if peer.error:
                raise peer.error
            self.assertEqual(outcomes, ["oversize", "oversize"])

    def test_real_unix_websocket_json_rpc_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "app-server.sock"
            peer = ScriptedPeer(socket_path)
            peer.start()
            self.assertTrue(peer.ready.wait(5), "scripted peer did not start")
            client = CodexAppServer(socket_path)
            try:
                result = client.thread_list({"limit": 100})
                messages = client.drain_server_messages()
            finally:
                client.close()
            peer.join(5)
            self.assertFalse(peer.is_alive(), "scripted peer did not shut down")
            if peer.error:
                raise peer.error
            self.assertEqual(result, {"data": [], "nextCursor": None})
            self.assertEqual(
                messages,
                [
                    {
                        "method": "thread/status/changed",
                        "params": {"threadId": "child-1", "status": {"type": "idle"}},
                    }
                ],
            )
            self.assertEqual([request["method"] for request in peer.requests], ["initialize", "initialized", "thread/list"])
            self.assertTrue(all(peer.masked), "every client frame must be masked")

    def test_native_board_polls_complete_read_only_passes_over_new_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "app-server.sock"
            peer = ScriptedPeer(socket_path, poll_statuses=("active", "idle", "idle"))
            peer.start()
            self.assertTrue(peer.ready.wait(5), "scripted peer did not start")
            board = NativeBoard(lambda: CodexAppServer(socket_path), "root-1")
            board.poll_once()
            active = board.snapshot()
            board.poll_once()
            idle = board.snapshot()
            trail_after_transition = idle["trail"]
            board.poll_once()
            unchanged = board.snapshot()
            peer.join(5)
            self.assertFalse(peer.is_alive(), "scripted peer did not shut down")
            if peer.error:
                raise peer.error
            self.assertEqual(
                [agent["status"] for agent in active["agents"]], ["active", "active"]
            )
            self.assertEqual(
                [agent["status"] for agent in idle["agents"]], ["idle", "idle"]
            )
            self.assertEqual(
                [entry["changes"] for entry in trail_after_transition[-2:]],
                [
                    {"status": {"from": "active", "to": "idle"}},
                    {"status": {"from": "active", "to": "idle"}},
                ],
            )
            self.assertEqual(unchanged["trail"], trail_after_transition)
            self.assertTrue(active["observation"]["available"])
            self.assertEqual(peer.connections, 3)
            list_requests = [
                request for request in peer.requests if request["method"] == "thread/list"
            ]
            self.assertEqual(len(list_requests), 6)
            list_params = [request.get("params") for request in list_requests]
            self.assertTrue(all(isinstance(params, dict) for params in list_params))
            self.assertTrue(
                all(
                    params.get("useStateDbOnly") is True
                    for params in list_params
                    if isinstance(params, dict)
                )
            )
            self.assertEqual(
                [request["method"] for request in peer.requests],
                ["initialize", "initialized", "thread/read", "thread/list", "thread/list"] * 3,
            )


if __name__ == "__main__":
    unittest.main()
