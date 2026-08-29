from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import http.client
import io
import json
from pathlib import Path
import threading
from typing import Any, cast
import unittest
from unittest.mock import patch

from switchstand.native_board import NativeBoard
from switchstand.native_http import (
    NativeHttpDispatcher,
    NativeHttpRequest,
    NativeHttpResponse,
)
from switchstand.service import (
    PACKAGE_ROOT,
    Runtime,
    Server,
    build_native_runtime,
    main,
)


class RecordingDispatcher:
    def __init__(self, response: NativeHttpResponse | None) -> None:
        self.response = response
        self.requests: list[NativeHttpRequest] = []

    def dispatch(self, request: NativeHttpRequest) -> NativeHttpResponse | None:
        self.requests.append(request)
        return self.response


class NoCallWorkbench:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.calls.append(name)
        raise AssertionError("domain port must not be called")


class DelegatingDispatcher(RecordingDispatcher):
    def __init__(self, delegate: NativeHttpDispatcher) -> None:
        super().__init__(None)
        self.delegate = delegate

    def dispatch(self, request: NativeHttpRequest) -> NativeHttpResponse | None:
        self.requests.append(request)
        return self.delegate.dispatch(request)


class EngineSpy:
    def __init__(self) -> None:
        self.reconciles = 0
        self.startup_deadline_seconds = 10.0
        self._clock = __import__("time").monotonic

    def reconcile_startup(self, _deadline: object) -> str:
        self.reconciles += 1
        return "completed"

    def reconcile_background(self) -> str:
        self.reconciles += 1
        return "completed"

    def reconcile_snapshot(self) -> dict[str, object]:
        self.reconciles += 1
        return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        return {"mode": "legacy", "reconciles": self.reconciles}


class LifecycleRuntime:
    def __init__(self, events: list[str], failure_stage: str | None = None) -> None:
        self.events = events
        self.failure_stage = failure_stage

    def start(self) -> None:
        self.events.append("runtime.start")
        if self.failure_stage == "start":
            raise RuntimeError("start failed")

    def close(self) -> None:
        self.events.append("runtime.close")
        if self.failure_stage == "runtime_close":
            raise RuntimeError("runtime close failed")


def lifecycle_server_type(
    events: list[str],
    failure_stage: str | None,
    *,
    interrupt: bool = False,
) -> type[Any]:
    class LifecycleServer:
        server_address = ("127.0.0.1", 54321)

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            events.append("server.bind")
            if failure_stage == "bind":
                raise RuntimeError("bind failed")

        def serve_forever(self, *, poll_interval: float) -> None:
            self.poll_interval = poll_interval
            events.append("server.serve")
            if interrupt or failure_stage in {"server_close", "runtime_close"}:
                raise KeyboardInterrupt
            raise RuntimeError("serve failed")

        def server_close(self) -> None:
            events.append("server.close")
            if failure_stage == "server_close":
                raise RuntimeError("server close failed")

    return LifecycleServer


class ServerHarness:
    def __init__(
        self,
        runtime: Runtime,
        dispatcher: RecordingDispatcher,
    ) -> None:
        self.server = Server(
            ("127.0.0.1", 0),
            runtime,
            PACKAGE_ROOT / "static",
            native_dispatcher=cast(NativeHttpDispatcher, dispatcher),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def exact_response(status: int = 207) -> NativeHttpResponse:
    body = b'{"exact":true}'
    return NativeHttpResponse(
        status,
        (
            ("Content-Type", "application/json"),
            ("X-Exact", "one"),
            ("X-Exact", "two"),
            ("Content-Length", str(len(body))),
        ),
        body,
    )


class NativeHandlerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = EngineSpy()
        self.runtime = Runtime(cast(Any, self.engine))

    def test_handled_get_post_and_options_preserve_exact_http_values(self) -> None:
        dispatcher = RecordingDispatcher(exact_response())
        harness = ServerHarness(self.runtime, dispatcher)
        try:
            connection = http.client.HTTPConnection("127.0.0.1", harness.port)
            connection.putrequest("POST", "/api/native-input?exact=query", skip_host=True)
            connection.putheader("Host", f"127.0.0.1:{harness.port}")
            connection.putheader("X-Repeated", "first")
            connection.putheader("X-Repeated", "second")
            body = b'{"text":"unchanged"}'
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            self.assertEqual(response.status, 207)
            self.assertEqual(response.read(), b'{"exact":true}')
            self.assertEqual(response.headers.get_all("X-Exact"), ["one", "two"])
            self.assertIsNone(response.getheader("Server"))
            self.assertIsNone(response.getheader("Date"))
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", harness.port)
            connection.request("GET", "/api/workbench?raw=%2Fvalue")
            get_response = connection.getresponse()
            self.assertEqual(get_response.read(), b'{"exact":true}')
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", harness.port)
            connection.request("OPTIONS", "/api/native-stop/prepare")
            options_response = connection.getresponse()
            self.assertEqual(options_response.read(), b'{"exact":true}')
            connection.close()
        finally:
            harness.close()

        post, get, options = dispatcher.requests
        self.assertEqual(post.method, "POST")
        self.assertEqual(post.path, "/api/native-input?exact=query")
        self.assertEqual(post.body, b'{"text":"unchanged"}')
        self.assertEqual(
            [item for item in post.headers if item[0].lower() == "x-repeated"],
            [("X-Repeated", "first"), ("X-Repeated", "second")],
        )
        self.assertEqual(get.path, "/api/workbench?raw=%2Fvalue")
        self.assertEqual(options.method, "OPTIONS")
        self.assertEqual(self.engine.reconciles, 0)

    def test_unhandled_post_reuses_the_buffered_body_and_static_get_is_unchanged(self) -> None:
        dispatcher = RecordingDispatcher(None)
        harness = ServerHarness(self.runtime, dispatcher)
        try:
            body = b'{"only":"one read"}'
            connection = http.client.HTTPConnection("127.0.0.1", harness.port)
            connection.request(
                "POST",
                "/api/workbench/reconcile",
                body,
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), {"mode": "legacy", "reconciles": 1})
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", harness.port)
            connection.request("GET", "/")
            static_response = connection.getresponse()
            self.assertEqual(static_response.status, 200)
            self.assertIn(b"Switchstand", static_response.read())
            connection.close()
        finally:
            harness.close()

        self.assertEqual(dispatcher.requests[0].body, body)
        self.assertEqual(self.engine.reconciles, 1)

    def test_oversize_body_is_not_read_and_untouched_length_reaches_dispatcher(self) -> None:
        dispatcher = RecordingDispatcher(exact_response(status=400))
        harness = ServerHarness(self.runtime, dispatcher)
        try:
            connection = http.client.HTTPConnection("127.0.0.1", harness.port)
            connection.putrequest("POST", "/api/native-input")
            connection.putheader("Content-Length", str(64 * 1024 + 1))
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            connection.close()
        finally:
            harness.close()

        request = dispatcher.requests[0]
        self.assertEqual(request.body, b"")
        self.assertIn(("Content-Length", str(64 * 1024 + 1)), request.headers)

    def test_extreme_content_length_is_fixed_rejection_without_domain_call(self) -> None:
        workbench = NoCallWorkbench()
        dispatcher = DelegatingDispatcher(
            NativeHttpDispatcher(cast(Any, workbench))
        )
        harness = ServerHarness(self.runtime, dispatcher)
        huge_length = "9" * 5000
        try:
            connection = http.client.HTTPConnection("127.0.0.1", harness.port)
            connection.putrequest("POST", "/api/native-input")
            connection.putheader("Origin", f"http://127.0.0.1:{harness.port}")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("X-Switchstand-Control", "native-input-v1")
            connection.putheader("Content-Length", huge_length)
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.load(response), {
                "code": "invalid_request", "outcome": "not_sent"
            })
            connection.close()
        finally:
            harness.close()

        self.assertEqual(workbench.calls, [])
        self.assertEqual(dispatcher.requests[0].body, b"")
        self.assertIn(("Content-Length", huge_length), dispatcher.requests[0].headers)


class NativeCompositionTests(unittest.TestCase):
    def test_server_rejects_a_native_board_without_the_single_dispatcher(self) -> None:
        board = NativeBoard(lambda: cast(Any, object()), "private-root")
        with self.assertRaisesRegex(ValueError, "requires the native HTTP dispatcher"):
            Server(("127.0.0.1", 0), board, PACKAGE_ROOT / "static")

    def test_one_exact_object_graph_receives_one_freshness_value(self) -> None:
        calls: list[tuple[object, ...]] = []

        class Board:
            def __init__(self, *args: object, **kwargs: object) -> None:
                calls.append(("board", args, kwargs))

            def resolve_current_target(self, *_args: object, **_kwargs: object) -> object:
                return object()

        board = Board()
        transport = object()
        native_input = object()
        workbench = object()
        dispatcher = object()
        with (
            patch("switchstand.service.NativeBoard", return_value=board) as board_type,
            patch("switchstand.service.NativeTargetTransport", return_value=transport) as transport_type,
            patch("switchstand.service.NativeInput", return_value=native_input) as input_type,
            patch("switchstand.service.NativeWorkbench", return_value=workbench) as workbench_type,
            patch("switchstand.service.NativeHttpDispatcher", return_value=dispatcher) as dispatcher_type,
        ):
            result = build_native_runtime(
                Path("/private/app-server.sock"),
                "private-root",
                maximum_observation_age_seconds=7.25,
            )

        self.assertEqual(result, (board, dispatcher))
        self.assertEqual(
            board_type.call_args.kwargs["maximum_observation_age_seconds"],
            7.25,
        )
        transport_type.assert_called_once_with(board, Path("/private/app-server.sock"))
        input_type.assert_called_once_with(
            board.resolve_current_target,
            transport,
            maximum_observation_age_seconds=7.25,
        )
        workbench_type.assert_called_once_with(
            board,
            board,
            native_input,
            board,
            maximum_observation_age_seconds=7.25,
        )
        dispatcher_type.assert_called_once_with(workbench)


class ServiceLifecycleTests(unittest.TestCase):
    def _argv(self) -> list[str]:
        return [
            "--app-server-socket",
            "/private/app-server.sock",
            "--native-root-thread-id",
            "private-root",
            "--port",
            "0",
        ]

    def test_port_zero_prints_bound_port_and_closes_once(self) -> None:
        events: list[str] = []
        output = io.StringIO()
        with (
            patch(
                "switchstand.service.build_native_runtime",
                return_value=(LifecycleRuntime(events), object()),
            ),
            patch(
                "switchstand.service.Server",
                lifecycle_server_type(events, None, interrupt=True),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(main(self._argv()), 0)

        self.assertIn("http://127.0.0.1:54321/", output.getvalue())
        self.assertEqual(events, [
            "server.bind", "runtime.start", "server.serve", "server.close", "runtime.close"
        ])

    def test_legacy_default_and_explicit_loopback_reach_construction(self) -> None:
        for host_args in ([], ["--host", "localhost"]):
            with self.subTest(host_args=host_args):
                events: list[str] = []
                output = io.StringIO()
                runtime = LifecycleRuntime(events)
                argv = [
                    "--app-server-socket",
                    "/private/app-server.sock",
                    "--port",
                    "0",
                    *host_args,
                ]
                with (
                    patch("switchstand.service.CodexAdapter", return_value=object()) as adapter,
                    patch("switchstand.service.Engine", return_value=object()) as engine,
                    patch("switchstand.service.Runtime", return_value=runtime) as runtime_type,
                    patch(
                        "switchstand.service.Server",
                        lifecycle_server_type(events, None, interrupt=True),
                    ),
                    redirect_stdout(output),
                ):
                    self.assertEqual(main(argv), 0)

                adapter.assert_called_once()
                engine.assert_called_once()
                runtime_type.assert_called_once()
                self.assertEqual(events, [
                    "server.bind", "runtime.start", "server.serve", "server.close", "runtime.close"
                ])

    def test_every_mode_rejects_unsafe_hosts_before_runtime_or_server_construction(self) -> None:
        unsafe_hosts = (
            ("wildcard", "0.0.0.0"),
            ("IPv6 wildcard", "::"),
            ("nonloopback", "192.0.2.10"),
            ("hostname", "example.com"),
            ("malformed", "localhost/path"),
        )
        for native_args in ([], ["--native-root-thread-id", "private-root"]):
            for label, host in unsafe_hosts:
                with self.subTest(mode="native" if native_args else "legacy", label=label):
                    stderr = io.StringIO()
                    argv = [
                        "--app-server-socket",
                        "/private/app-server.sock",
                        "--host",
                        host,
                        *native_args,
                    ]
                    with (
                        patch("switchstand.service.CodexAdapter") as adapter,
                        patch("switchstand.service.Engine") as engine,
                        patch("switchstand.service.Runtime") as runtime_type,
                        patch("switchstand.service.build_native_runtime") as native_runtime,
                        patch("switchstand.service.Server") as server,
                        redirect_stderr(stderr),
                        self.assertRaises(SystemExit) as raised,
                    ):
                        main(argv)

                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn("Switchstand requires a loopback --host", stderr.getvalue())
                    adapter.assert_not_called()
                    engine.assert_not_called()
                    runtime_type.assert_not_called()
                    native_runtime.assert_not_called()
                    server.assert_not_called()

    def test_runtime_closes_once_after_bind_start_or_serve_failure(self) -> None:
        for failure_stage in ("bind", "start", "serve"):
            with self.subTest(stage=failure_stage):
                events: list[str] = []
                output = io.StringIO()
                with (
                    patch(
                        "switchstand.service.build_native_runtime",
                        return_value=(LifecycleRuntime(events, failure_stage), object()),
                    ),
                    patch(
                        "switchstand.service.Server",
                        lifecycle_server_type(events, failure_stage),
                    ),
                    redirect_stdout(output),
                    self.assertRaises(RuntimeError),
                ):
                    main(self._argv())

                self.assertEqual(events.count("runtime.close"), 1)
                self.assertEqual(events.count("server.close"), 0 if failure_stage == "bind" else 1)
                if failure_stage != "bind":
                    self.assertLess(events.index("server.bind"), events.index("runtime.start"))

    def test_both_closes_are_attempted_once_and_shutdown_failure_propagates(self) -> None:
        cases = (
            ("server_close", "server close failed"),
            ("runtime_close", "runtime close failed"),
        )
        for failure_stage, expected_error in cases:
            with self.subTest(stage=failure_stage):
                events: list[str] = []
                output = io.StringIO()
                with (
                    patch(
                        "switchstand.service.build_native_runtime",
                        return_value=(LifecycleRuntime(events, failure_stage), object()),
                    ),
                    patch(
                        "switchstand.service.Server",
                        lifecycle_server_type(events, failure_stage),
                    ),
                    redirect_stdout(output),
                    self.assertRaisesRegex(RuntimeError, expected_error),
                ):
                    main(self._argv())

                self.assertEqual(events.count("server.close"), 1)
                self.assertEqual(events.count("runtime.close"), 1)


if __name__ == "__main__":
    unittest.main()
