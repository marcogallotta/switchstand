from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, cast
from unittest.mock import patch
import unittest

from switchstand.engine import Engine
from switchstand.legacy_deadline import PhaseResult
from switchstand.service import PACKAGE_ROOT, Runtime, Server, main
from tests.test_legacy_deadline import ScriptedAdapter


class RuntimeStub:
    def start(self) -> None:
        pass

    def close(self) -> None:
        pass


class ServerStub:
    server_address = ("127.0.0.1", 43210)

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def serve_forever(self, *, poll_interval: float) -> None:
        raise KeyboardInterrupt

    def server_close(self) -> None:
        pass


class LegacyServiceConfigurationTests(unittest.TestCase):
    def argv(self, *extra: str) -> list[str]:
        return ["--app-server-socket", "/private/socket", "--port", "0", *extra]

    def test_legacy_cli_precedes_environment_and_defaults_are_exact(self) -> None:
        for extra, environment, expected in (
            ((), {}, (10.0, 5.0)),
            ((), {
                "SWITCHSTAND_LEGACY_STARTUP_DEADLINE_SECONDS": "11",
                "SWITCHSTAND_LEGACY_OPERATION_DEADLINE_SECONDS": "6",
            }, (11.0, 6.0)),
            (("--legacy-startup-deadline-seconds", "12", "--legacy-operation-deadline-seconds", "7"), {
                "SWITCHSTAND_LEGACY_STARTUP_DEADLINE_SECONDS": "13",
                "SWITCHSTAND_LEGACY_OPERATION_DEADLINE_SECONDS": "8",
            }, (12.0, 7.0)),
        ):
            with self.subTest(extra=extra, environment=environment):
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch("switchstand.service.CodexAdapter", return_value=object()),
                    patch("switchstand.service.Engine", return_value=object()) as engine,
                    patch("switchstand.service.Runtime", return_value=RuntimeStub()),
                    patch("switchstand.service.Server", ServerStub),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(main(self.argv(*extra)), 0)
                kwargs = engine.call_args.kwargs
                self.assertEqual(
                    (kwargs["startup_deadline_seconds"], kwargs["operation_deadline_seconds"]),
                    expected,
                )

    def test_invalid_legacy_environment_fails_before_runtime_construction(self) -> None:
        for value in ("", "nan", "inf", "0", "301", "not-a-number"):
            with self.subTest(value=value):
                stderr = io.StringIO()
                with (
                    patch.dict(
                        os.environ,
                        {"SWITCHSTAND_LEGACY_STARTUP_DEADLINE_SECONDS": value},
                        clear=True,
                    ),
                    patch("switchstand.service.CodexAdapter") as adapter,
                    patch("switchstand.service.Engine") as engine,
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    main(self.argv())
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("greater than 0", stderr.getvalue())
                adapter.assert_not_called()
                engine.assert_not_called()

    def test_native_ignores_poisoned_environment_and_rejects_explicit_flags(self) -> None:
        poison = {
            "SWITCHSTAND_LEGACY_STARTUP_DEADLINE_SECONDS": "poison",
            "SWITCHSTAND_LEGACY_OPERATION_DEADLINE_SECONDS": "poison",
        }
        with (
            patch.dict(os.environ, poison, clear=True),
            patch("switchstand.service.build_native_runtime", return_value=(RuntimeStub(), object())) as native,
            patch("switchstand.service.Server", ServerStub),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(self.argv("--native-root-thread-id", "root")), 0)
        native.assert_called_once()

        for flag in ("--legacy-startup-deadline-seconds", "--legacy-operation-deadline-seconds"):
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, poison, clear=True),
                patch("switchstand.service.build_native_runtime") as native,
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                main(self.argv("--native-root-thread-id", "root", flag, "1"))
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("unavailable in native mode", stderr.getvalue())
            native.assert_not_called()


class LegacyHttpDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.adapter = ScriptedAdapter()
        self.engine = Engine(
            Path(self.temp.name) / "state.json",
            self.adapter,
            operation_deadline_seconds=0.03,
        )
        self.runtime = Runtime(self.engine)
        self.server = Server(("127.0.0.1", 0), self.runtime, PACKAGE_ROOT / "static")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)
        self.temp.cleanup()

    def request(self, method: str, path: str, body: object | None = None) -> tuple[int, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        encoded = None if body is None else json.dumps(body)
        connection.request(method, path, encoded, {"Content-Type": "application/json"})
        response = connection.getresponse()
        value = json.load(response)
        status = response.status
        connection.close()
        return status, value

    def test_preacceptance_get_and_post_cutoff_are_fixed_503_without_mutation(self) -> None:
        before = self.engine.state_path.read_bytes(), self.engine.events_path.read_bytes()
        held = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with self.engine._lock:
                held.set()
                release.wait(2)

        lock_thread = threading.Thread(target=holder)
        lock_thread.start()
        self.assertTrue(held.wait(1))
        try:
            self.assertEqual(
                self.request("GET", "/api/workbench"),
                (503, {"error": "legacy_deadline_exceeded"}),
            )
            self.assertEqual(
                self.request("POST", "/api/workbench/roles/role-a/messages", {"text": "blocked"}),
                (503, {"error": "legacy_deadline_exceeded"}),
            )
            self.assertEqual((self.engine.state_path.read_bytes(), self.engine.events_path.read_bytes()), before)
            self.assertEqual(self.adapter.calls, [])
        finally:
            release.set()
            lock_thread.join(1)

    def test_postacceptance_partial_state_is_200_and_persistence_latch_is_fixed_503(self) -> None:
        self.adapter.start = PhaseResult("not_sent", "turn/start", code="setup_cutoff")
        status, state = self.request(
            "POST",
            "/api/workbench/roles/role-a/messages",
            {"text": "accepted"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(state["messages"][0]["status"], "queued")
        self.engine.persistence_failed = True
        self.assertEqual(
            self.request("GET", "/api/workbench"),
            (503, {"error": "legacy_persistence_unavailable"}),
        )

    def test_response_snapshot_is_built_while_the_owning_lock_is_held(self) -> None:
        original = self.engine._snapshot_locked
        observed: list[bool] = []

        def audited_snapshot():
            owned = cast(Any, self.engine._lock)._is_owned
            observed.append(bool(owned()))
            time.sleep(0.01)
            return original()

        self.engine._snapshot_locked = audited_snapshot
        status, _ = self.request("POST", "/api/workbench/reconcile", {})
        self.assertEqual(status, 200)
        self.assertEqual(observed, [True])

    def test_response_snapshot_cannot_interleave_with_a_waiting_mutation(self) -> None:
        original = self.engine._snapshot_locked
        snapshot_entered = threading.Event()
        release_snapshot = threading.Event()

        def held_snapshot():
            value = original()
            snapshot_entered.set()
            self.assertTrue(release_snapshot.wait(1))
            return value

        self.engine._snapshot_locked = held_snapshot
        response: list[tuple[int, Any]] = []
        request_thread = threading.Thread(
            target=lambda: response.append(self.request("POST", "/api/workbench/reconcile", {}))
        )
        request_thread.start()
        self.assertTrue(snapshot_entered.wait(1))
        mutation_done = threading.Event()

        def mutate() -> None:
            self.engine.enqueue("role-b", "must wait")
            mutation_done.set()

        mutation_thread = threading.Thread(target=mutate)
        mutation_thread.start()
        self.assertFalse(mutation_done.wait(0.005))
        release_snapshot.set()
        request_thread.join(1)
        mutation_thread.join(1)
        self.assertEqual(response[0][0], 200)
        self.assertEqual(response[0][1]["messages"], [])
        self.assertTrue(mutation_done.is_set())


if __name__ == "__main__":
    unittest.main()
