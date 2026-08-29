from __future__ import annotations

from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

from switchstand.agent_tree import AgentTreeAdapter
from switchstand.app_server import CodexAppServer
from tests.integration.app_server_transport_test import (
    ScriptedPeer,
    receive_frame,
    send_json,
)


class ObservationFailurePeer(ScriptedPeer):
    def __init__(
        self,
        path: Path,
        *,
        page_padding: int = 0,
        root_delay: float = 0.0,
        page_delay: float = 0.0,
    ) -> None:
        super().__init__(path)
        self.page_padding = page_padding
        self.root_delay = root_delay
        self.page_delay = page_delay

    def run(self) -> None:
        server = None
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.path))
            server.listen(1)
            server.settimeout(5)
            self.ready.set()
            with self.upgrade(server) as connection:
                root = self.message(connection)
                self.assert_equal(root["method"], "thread/read")
                threading.Event().wait(self.root_delay)
                send_json(
                    connection,
                    {
                        "id": root["id"],
                        "result": {"thread": self.thread("root-1", None, "idle")},
                    },
                )
                listing = self.message(connection)
                self.assert_equal(listing["method"], "thread/list")
                threading.Event().wait(self.page_delay)
                result = {"data": [], "nextCursor": None}
                if self.page_padding:
                    result["padding"] = "x" * self.page_padding
                try:
                    send_json(connection, {"id": listing["id"], "result": result})
                    receive_frame(connection)
                except OSError:
                    pass
        except BaseException as exc:
            self.error = exc
            self.ready.set()
        finally:
            if server is not None:
                server.close()


class SlowInitializePeer(ScriptedPeer):
    def run(self) -> None:
        server = None
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.path))
            server.listen(1)
            server.settimeout(5)
            self.ready.set()
            with self.upgrade(server, initialize_client=False) as connection:
                initialize = self.message(connection)
                threading.Event().wait(0.5)
                try:
                    send_json(connection, {"id": initialize["id"], "result": {}})
                except OSError:
                    pass
        except BaseException as exc:
            self.error = exc
            self.ready.set()
        finally:
            if server is not None:
                server.close()


class NativeObservationTransportTest(unittest.TestCase):
    def test_setup_root_and_page_share_one_byte_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "observation-budget.sock"
            peer = ObservationFailurePeer(socket_path, page_padding=2_000)
            peer.start()
            self.assertTrue(peer.ready.wait(5))
            client = CodexAppServer(
                socket_path,
                timeout_seconds=3,
                bounded_stop=True,
                bounded_response_bytes=1_024,
            )

            with self.assertRaisesRegex(RuntimeError, "observation is unavailable"):
                AgentTreeAdapter(client).observe_tree("root-1")

            peer.join(5)
            if peer.error:
                raise peer.error
            self.assertEqual(
                [request["method"] for request in peer.requests],
                ["initialize", "initialized", "thread/read", "thread/list"],
            )

    def test_setup_root_and_page_share_one_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "observation-deadline.sock"
            peer = ObservationFailurePeer(
                socket_path,
                root_delay=0.2,
                page_delay=0.7,
            )
            peer.start()
            self.assertTrue(peer.ready.wait(5))
            started = time.monotonic()
            client = CodexAppServer(
                socket_path,
                timeout_seconds=0.8,
                bounded_stop=True,
            )

            with self.assertRaisesRegex(RuntimeError, "observation is unavailable"):
                AgentTreeAdapter(client).observe_tree("root-1")
            elapsed = time.monotonic() - started

            peer.join(5)
            if peer.error:
                raise peer.error
            self.assertLess(elapsed, 1.2)
            self.assertEqual(
                [request["method"] for request in peer.requests],
                ["initialize", "initialized", "thread/read", "thread/list"],
            )

    def test_bounded_startup_timeout_is_prompt_and_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "startup-timeout.sock"
            peer = SlowInitializePeer(socket_path)
            peer.start()
            self.assertTrue(peer.ready.wait(5))
            started = time.monotonic()

            with self.assertRaisesRegex(RuntimeError, "^setup_unavailable$"):
                CodexAppServer(
                    socket_path,
                    timeout_seconds=0.2,
                    bounded_stop=True,
                )
            elapsed = time.monotonic() - started

            peer.join(5)
            if peer.error:
                raise peer.error
            self.assertLess(elapsed, 0.4)
            self.assertEqual(
                [request["method"] for request in peer.requests],
                ["initialize"],
            )


if __name__ == "__main__":
    unittest.main()
