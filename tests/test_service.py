from __future__ import annotations

from http.client import HTTPConnection
from pathlib import Path
import threading
from typing import Any, cast
import unittest

from switchstand.service import NativeRuntime, PACKAGE_ROOT, Server


class FakeObserver:
    def snapshot(self):
        return {"mode": "native", "readOnly": True, "threads": []}


class NativeServiceTests(unittest.TestCase):
    def test_native_api_is_read_only_and_returns_only_observer_state(self):
        runtime = NativeRuntime(cast(Any, FakeObserver()))
        server = Server(("127.0.0.1", 0), runtime, Path(PACKAGE_ROOT / "static"))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
        try:
            connection.request("GET", "/api/workbench")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b'{"mode": "native", "readOnly": true, "threads": []}')

            connection.request(
                "POST",
                "/api/workbench/roles/private/messages",
                body=b'{"text":"must not dispatch"}',
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 405)
            self.assertEqual(response.read(), b'{"error": "native_mode_read_only"}')
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == "__main__":
    unittest.main()
