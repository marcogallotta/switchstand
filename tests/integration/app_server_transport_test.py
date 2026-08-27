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


def send_json(connection: socket.socket, value: object) -> None:
    payload = json.dumps(value, separators=(",", ":")).encode()
    if len(payload) < 126:
        header = bytes((0x81, len(payload)))
    elif len(payload) <= 0xFFFF:
        header = bytes((0x81, 126)) + struct.pack("!H", len(payload))
    else:
        header = bytes((0x81, 127)) + struct.pack("!Q", len(payload))
    connection.sendall(header + payload)


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

    def upgrade(self, server: socket.socket) -> socket.socket:
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


class AppServerTransportTest(unittest.TestCase):
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
