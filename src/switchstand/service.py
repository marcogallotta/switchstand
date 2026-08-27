"""Dependency-free local HTTP service for Switchstand."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from typing import Any, cast
from urllib.parse import unquote, urlsplit

from .app_server import CodexAppServer
from .engine import CodexAdapter, Engine
from .native_board import (
    DEFAULT_MAXIMUM_OBSERVATION_AGE_SECONDS,
    NativeBoard,
)
from .native_http import (
    NativeHttpDispatcher,
    NativeHttpRequest,
    NativeHttpResponse,
)
from .native_http_contract import (
    CONTROL_REQUEST_REJECTED_BODY,
    MAX_BODY_BYTES,
    is_loopback_host as _loopback,
)
from .native_input import NativeInput
from .native_target_transport import NativeTargetTransport
from .native_workbench import NativeWorkbench


PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_STATE = Path.home() / ".local" / "state" / "switchstand" / "state.json"
class Runtime:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.stop_event = threading.Event()
        self.observer = threading.Thread(target=self._observe, name="switchstand-observer", daemon=True)

    def start(self) -> None:
        self.engine.reconcile()
        self.observer.start()

    def close(self) -> None:
        self.stop_event.set()
        self.observer.join(timeout=2)

    def _observe(self) -> None:
        while not self.stop_event.wait(0.5):
            self.engine.reconcile()

    def snapshot(self) -> dict[str, Any]:
        return self.engine.snapshot()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: int, value: Any) -> None:
        body = json.dumps(value, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _native_response(self, response: NativeHttpResponse) -> None:
        self.send_response_only(response.status)
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response.body)

    def _native_request(self, body: bytes = b"") -> NativeHttpRequest:
        return NativeHttpRequest(
            method=self.command,
            path=self.path,
            headers=tuple(self.headers.raw_items()),
            body=body,
        )

    def _bounded_raw_body(self) -> bytes:
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1:
            if lengths:
                self.close_connection = True
            return b""
        raw_length = lengths[0]
        if (
            not raw_length
            or not raw_length.isascii()
            or not raw_length.isdecimal()
            or len(raw_length) > len(str(MAX_BODY_BYTES))
        ):
            self.close_connection = True
            return b""
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            return b""
        return self.rfile.read(length)

    def _dispatch_native(self, body: bytes = b"") -> bool:
        dispatcher = cast("Server", self.server).native_dispatcher
        if dispatcher is None:
            return False
        response = dispatcher.dispatch(self._native_request(body))
        if response is None:
            return False
        self._native_response(response)
        return True

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    @staticmethod
    def _decode_json(body: bytes) -> dict[str, Any]:
        value = json.loads(body or b"{}")
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _static(self, pathname: str) -> None:
        relative = "index.html" if pathname in {"/", "/workbench"} else unquote(pathname).lstrip("/")
        root = cast("Server", self.server).static_root.resolve()
        candidate = (root / relative).resolve()
        if root not in candidate.parents or not candidate.is_file():
            if "." not in Path(relative).name:
                candidate = root / "index.html"
            else:
                self.send_error(404)
                return
        content_type = {
            ".css": "text/css; charset=utf-8",
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
        }.get(candidate.suffix, "application/octet-stream")
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self._dispatch_native():
            return
        pathname = urlsplit(self.path).path
        runtime = cast("Server", self.server).runtime
        if pathname.startswith("/api/native-stop/") and isinstance(runtime, Runtime):
            self._json(405, CONTROL_REQUEST_REJECTED_BODY)
            return
        if pathname == "/api/workbench" and isinstance(runtime, Runtime):
            self._json(200, runtime.snapshot())
            return
        self._static(pathname)

    def do_OPTIONS(self) -> None:
        if self._dispatch_native():
            return
        self.send_error(501, "Unsupported method ('OPTIONS')")

    def do_POST(self) -> None:
        server = cast("Server", self.server)
        if server.native_dispatcher is not None:
            raw_body = self._bounded_raw_body()
            if self._dispatch_native(raw_body):
                return
            try:
                body = self._decode_json(raw_body)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(409, {"error": str(exc)})
                return
        else:
            try:
                body = self._read_json()
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(409, {"error": str(exc)})
                return
        pathname = urlsplit(self.path).path
        runtime = server.runtime
        try:
            parts = [part for part in pathname.split("/") if part]
            if not isinstance(runtime, Runtime):
                self._json(404, {"error": "operation_unavailable_in_native_mode"})
                return
            engine = runtime.engine
            if len(parts) == 5 and parts[:2] == ["api", "workbench"] and parts[2] == "roles" and parts[4] == "messages":
                engine.enqueue(parts[3], str(body.get("text") or ""), kind=str(body.get("kind") or "message"))
            elif len(parts) == 5 and parts[:3] == ["api", "workbench", "attempts"] and parts[4] == "stop":
                engine.stop(parts[3])
            elif len(parts) == 5 and parts[:3] == ["api", "workbench", "attempts"] and parts[4] == "replace":
                engine.replace(parts[3])
            elif len(parts) == 5 and parts[:3] == ["api", "workbench", "attempts"] and parts[4] == "redirect":
                engine.redirect(parts[3], str(body.get("text") or ""))
            elif pathname == "/api/workbench/reconcile":
                engine.reconcile()
            else:
                self._json(404, {"error": "not_found"})
                return
            self._json(200, engine.snapshot())
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self._json(409, {"error": str(exc)})
        except Exception as exc:
            self._json(503, {"error": str(exc)})


class Server(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        runtime: Runtime | NativeBoard,
        static_root: Path,
        *,
        native_dispatcher: NativeHttpDispatcher | None = None,
    ) -> None:
        if isinstance(runtime, NativeBoard) and native_dispatcher is None:
            raise ValueError("native runtime requires the native HTTP dispatcher")
        super().__init__(address, Handler)
        self.runtime = runtime
        self.static_root = static_root
        self.native_dispatcher = native_dispatcher


def build_native_runtime(
    socket_path: Path,
    root_thread_id: str,
    *,
    maximum_observation_age_seconds: float,
) -> tuple[NativeBoard, NativeHttpDispatcher]:
    """Compose the merged native ports with one shared freshness setting."""
    board = NativeBoard(
        lambda: CodexAppServer(
            socket_path,
            timeout_seconds=3,
            bounded_stop=True,
        ),
        root_thread_id,
        maximum_observation_age_seconds=maximum_observation_age_seconds,
    )
    transport = NativeTargetTransport(board, socket_path)
    native_input = NativeInput(
        board.resolve_current_target,
        transport,
        maximum_observation_age_seconds=maximum_observation_age_seconds,
    )
    workbench = NativeWorkbench(
        board,
        board,
        native_input,
        board,
        maximum_observation_age_seconds=maximum_observation_age_seconds,
    )
    return board, NativeHttpDispatcher(workbench)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Switchstand prototype")
    parser.add_argument("--state", type=Path, default=Path(os.getenv("SWITCHSTAND_STATE") or DEFAULT_STATE))
    parser.add_argument("--app-server-socket", type=Path, default=os.getenv("SWITCHSTAND_APP_SERVER_SOCKET"))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--static-root", type=Path, default=PACKAGE_ROOT / "static")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT") or "4180"))
    parser.add_argument("--role-a", default="Role A")
    parser.add_argument("--role-b", default="Role B")
    parser.add_argument("--native-root-thread-id")
    parser.add_argument(
        "--maximum-observation-age-seconds",
        type=float,
        default=DEFAULT_MAXIMUM_OBSERVATION_AGE_SECONDS,
        help="native complete-pass freshness limit (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.app_server_socket is None:
        parser.error("--app-server-socket or SWITCHSTAND_APP_SERVER_SOCKET is required")
    if not args.static_root.is_dir():
        parser.error(f"static files are missing at {args.static_root}")

    if args.native_root_thread_id:
        if not _loopback(args.host):
            parser.error("native mode requires a loopback --host")
        runtime, native_dispatcher = build_native_runtime(
            args.app_server_socket,
            args.native_root_thread_id,
            maximum_observation_age_seconds=args.maximum_observation_age_seconds,
        )
    else:
        adapter = CodexAdapter(args.app_server_socket, cwd=args.workspace)
        runtime = Runtime(Engine(args.state, adapter, role_names=(args.role_a, args.role_b)))
        native_dispatcher = None
    server: Server | None = None
    try:
        server = Server(
            (args.host, args.port),
            runtime,
            args.static_root,
            native_dispatcher=native_dispatcher,
        )
        runtime.start()
        print(f"Switchstand: http://{args.host}:{server.server_address[1]}/", flush=True)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if server is not None:
                server.server_close()
        finally:
            runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
