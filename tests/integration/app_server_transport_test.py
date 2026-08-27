from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
from typing import cast
import unittest

from switchstand.app_server import CodexAppServer
from switchstand.native_observer import NativeObserver


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
    def __init__(self, path: Path, *, observer_status: str | None = None) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.requests: list[dict[str, object]] = []
        self.masked: list[bool] = []
        self.observer_status = observer_status

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

    def run(self) -> None:
        server = None
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.path))
            server.listen(1)
            server.settimeout(5)
            self.ready.set()
            connection, _ = server.accept()
            with connection:
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
                if self.observer_status is not None:
                    self.observer_pass(connection)
                    return
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

    def observer_pass(self, connection: socket.socket) -> None:
        reading = self.message(connection)
        self.assert_equal(reading["method"], "thread/read")
        self.assert_equal(reading["params"], {"threadId": "root-live", "includeTurns": False})
        send_json(
            connection,
            {
                "id": reading["id"],
                "result": {
                    "thread": {
                        "id": "root-live",
                        "sessionId": "session-root",
                        "parentThreadId": None,
                        "source": "cli",
                        "createdAt": 10,
                        "updatedAt": 20,
                        "status": {"type": "idle"},
                    }
                },
            },
        )
        listing = self.message(connection)
        self.assert_equal(listing["method"], "thread/list")
        listing_params = cast(dict[str, object], listing["params"])
        self.assert_equal(listing_params["useStateDbOnly"], True)
        if self.observer_status == "disconnect":
            return
        send_json(
            connection,
            {
                "id": listing["id"],
                "result": {
                    "data": [
                        {
                            "id": "child-live",
                            "sessionId": "session-child",
                            "parentThreadId": "root-live",
                            "source": {"subAgent": "review"},
                            "createdAt": 11,
                            "updatedAt": 25 if self.observer_status == "active" else 30,
                            "status": {
                                "type": self.observer_status,
                                **({"activeFlags": ["waitingOnUserInput"]} if self.observer_status == "active" else {}),
                            },
                        }
                    ],
                    "nextCursor": "page-2",
                },
            },
        )
        if self.observer_status == "invalid":
            opcode, _, masked = receive_frame(connection)
            self.masked.append(masked)
            self.assert_equal(opcode, 8)
            return
        final_page = self.message(connection)
        self.assert_equal(final_page["method"], "thread/list")
        final_params = cast(dict[str, object], final_page["params"])
        self.assert_equal(final_params["cursor"], "page-2")
        self.assert_equal(final_params["useStateDbOnly"], True)
        send_json(connection, {"id": final_page["id"], "result": {"data": [], "nextCursor": None}})
        opcode, _, masked = receive_frame(connection)
        self.masked.append(masked)
        self.assert_equal(opcode, 8)


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

    def test_native_observer_recovers_real_socket_disconnect_and_exhausts_db_only_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "observer.sock"
            peers = []
            states = []
            clock = iter([100.0, 105.0, 110.0])
            observer = NativeObserver(
                lambda: CodexAppServer(socket_path),
                "root-live",
                clock=lambda: next(clock),
            )
            for status in ("disconnect", "active", "invalid", "active", "idle"):
                peer = ScriptedPeer(socket_path, observer_status=status)
                peers.append(peer)
                peer.start()
                self.assertTrue(peer.ready.wait(5), "scripted observer peer did not start")
                observer.observe_once()
                states.append(observer.snapshot())
                peer.join(5)
                self.assertFalse(peer.is_alive(), "scripted observer peer did not shut down")
                if peer.error:
                    raise peer.error
                if socket_path.exists():
                    socket_path.unlink()

            state = observer.snapshot()
            self.assertTrue(state["observation"]["connected"])
            self.assertEqual(state["threads"][1]["status"], {"type": "idle"})
            self.assertEqual(
                states[1]["threads"][1]["status"],
                {"type": "active", "activeFlags": ["waitingOnUserInput"]},
            )
            self.assertEqual(states[1]["threads"][1]["activeObservedSeconds"], 0)
            self.assertTrue(states[2]["observation"]["historical"])
            self.assertEqual(states[2]["observation"]["errorCode"], "native_observation_unavailable")
            self.assertEqual(states[3]["threads"][1]["activeObservedSeconds"], 0)
            methods = [[request["method"] for request in peer.requests] for peer in peers]
            self.assertEqual(methods[0], ["initialize", "initialized", "thread/read", "thread/list"])
            self.assertEqual(methods[2], ["initialize", "initialized", "thread/read", "thread/list"])
            for method_list in (methods[1], methods[3], methods[4]):
                self.assertEqual(
                    method_list,
                    ["initialize", "initialized", "thread/read", "thread/list", "thread/list"],
                )
            self.assertTrue(all(set(items) <= {"initialize", "initialized", "thread/read", "thread/list"} for items in methods))
            self.assertTrue(all(all(peer.masked) for peer in peers))


if __name__ == "__main__":
    unittest.main()
