"""Dependency-free local HTTP service for Switchstand."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import threading
from typing import Any, cast
from urllib.parse import unquote, urlsplit

from .app_server import CodexAppServer
from .engine import CodexAdapter, Engine
from .native_board import NativeBoard


PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_STATE = Path.home() / ".local" / "state" / "switchstand" / "state.json"
MAX_BODY_BYTES = 64 * 1024
NATIVE_HEADER = "native-stop-v1"


def _loopback(value: str | None) -> bool:
    if not value:
        return False
    try:
        hostname = urlsplit(f"//{value}").hostname
        return hostname == "localhost" or (
            hostname is not None and ipaddress.ip_address(hostname).is_loopback
        )
    except ValueError:
        return False


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
        pathname = urlsplit(self.path).path
        if pathname.startswith("/api/native-stop/"):
            self._json(405, {"code": "control_request_rejected", "outcome": "not_sent"}); return
        if pathname == "/api/workbench":
            self._json(200, cast("Server", self.server).runtime.snapshot())
            return
        self._static(pathname)

    def do_POST(self) -> None:
        pathname = urlsplit(self.path).path
        runtime = cast("Server", self.server).runtime
        native_actions = {"/api/native-stop/prepare": "prepare_stop",
            "/api/native-stop/commit": "commit_stop", "/api/native-stop/status": "stop_status"}
        if isinstance(runtime, NativeBoard) and pathname in native_actions:
            host, origin = self.headers.get("Host"), self.headers.get("Origin")
            origin_ok = origin is None or (origin != "null" and urlsplit(origin).scheme == "http"
                and urlsplit(origin).netloc.lower() == (host or "").lower())
            if (self.headers.get("Content-Type") != "application/json"
                    or self.headers.get("X-Switchstand-Control") != NATIVE_HEADER
                    or not _loopback(host) or not origin_ok):
                self._json(403, {"code": "control_request_rejected", "outcome": "not_sent"})
                return
            try:
                body = self._read_json()
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"code": "invalid_request", "outcome": "not_sent"})
                return
            keys = {"prepare_stop": "agentRef", "commit_stop": "confirmationRef",
                "stop_status": "operationRef"}
            key = keys[native_actions[pathname]]
            result = getattr(runtime, native_actions[pathname])(body.get(key))
            self._json(200, result)
            return
        try:
            body = self._read_json()
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
    def __init__(self, address: tuple[str, int], runtime: Runtime | NativeBoard, static_root: Path) -> None:
        super().__init__(address, Handler)
        self.runtime = runtime
        self.static_root = static_root


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
        runtime: Runtime | NativeBoard = NativeBoard(
            lambda: CodexAppServer(args.app_server_socket, timeout_seconds=3, bounded_stop=True), args.native_root_thread_id
        )
    else:
        adapter = CodexAdapter(args.app_server_socket, cwd=args.workspace)
        runtime = Runtime(Engine(args.state, adapter, role_names=(args.role_a, args.role_b)))
    runtime.start()
    server = Server((args.host, args.port), runtime, args.static_root)
    print(f"Switchstand: http://{args.host}:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
