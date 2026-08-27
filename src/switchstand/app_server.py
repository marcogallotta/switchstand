"""Dependency-free Codex app-server client for a Unix WebSocket socket.

This module intentionally implements only the methods used by Switchstand.
"""
from __future__ import annotations

import base64
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import threading
import time
from typing import Any, Mapping


_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class _StopFailure(RuntimeError): pass


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class CodexAppServer:
    """Small synchronous JSON-RPC client for Codex app-server v2."""

    def __init__(
        self,
        socket_path: Path | str,
        *,
        client_name: str = "switchstand",
        client_title: str = "Switchstand",
        timeout_seconds: float | None = None,
        bounded_stop: bool = False,
        bounded_response_bytes: int = 256 * 1024,
    ) -> None:
        if bounded_stop and (type(bounded_response_bytes) is not int or bounded_response_bytes <= 0):
            raise ValueError("bounded response byte cap must be positive")
        self._bounded_deadline = (
            time.monotonic() + timeout_seconds
            if bounded_stop and timeout_seconds is not None
            else None
        )
        self._bounded_response_bytes_remaining = (
            bounded_response_bytes if bounded_stop else None
        )
        self.socket_path = Path(socket_path)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout_seconds)
        if not bounded_stop:
            self.socket.connect(str(self.socket_path))
        else:
            failed = False
            try:
                self.socket.connect(str(self.socket_path))
            except Exception:
                self.socket.close()
                failed = True
            if failed:
                raise _StopFailure("setup_unavailable")
        self._next_id = 0
        self._lock = threading.Lock()
        self._server_messages: deque[dict[str, Any]] = deque()
        if not bounded_stop:
            self.reader = self.socket.makefile("rb")
        else:
            failed = False
            try:
                self.reader = self.socket.makefile("rb")
            except Exception:
                self.socket.close()
                failed = True
            if failed:
                raise _StopFailure("setup_unavailable")
        initialize = {
                "clientInfo": {
                    "name": client_name,
                    "title": client_title,
                    "version": "1",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": [
                        "item/agentMessage/delta",
                        "item/reasoning/summaryTextDelta",
                        "thread/tokenUsage/updated",
                    ],
                },
            }
        setup_failed = False
        try:
            upgrade_bytes = self._upgrade(
                min(64 * 1024, bounded_response_bytes) if bounded_stop else None,
                timeout_seconds if bounded_stop else None,
                deadline=self._bounded_deadline,
            )
            self._consume_bounded_response_bytes(upgrade_bytes)
            if bounded_stop:
                classification, _ = self.bounded_request(
                    "initialize", initialize, _close_after=False
                )
                if classification != "ok":
                    raise _StopFailure("setup_unavailable")
            else:
                self._request("initialize", initialize)
            self._notify("initialized", {})
        except Exception:
            if not bounded_stop:
                raise
            self._stop_close()
            setup_failed = True
        if setup_failed:
            raise _StopFailure("setup_unavailable")

    def _upgrade(
        self,
        max_header_bytes: int | None = None,
        timeout_seconds: float | None = None,
        *,
        deadline: float | None = None,
    ) -> int:
        if deadline is None and timeout_seconds is not None:
            deadline = time.monotonic() + timeout_seconds
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.socket.settimeout(remaining)
        self.socket.sendall(request)
        status_line = self._read_line(max_header_bytes, deadline)
        used = len(status_line)
        if max_header_bytes is not None and used > max_header_bytes:
            raise _StopFailure
        status = status_line.decode("ascii", errors="replace").strip()
        headers: dict[str, str] = {}
        while True:
            line = self._read_line(
                None if max_header_bytes is None else max_header_bytes - used,
                deadline,
            )
            used += len(line)
            if max_header_bytes is not None and used > max_header_bytes:
                raise _StopFailure
            if not line:
                raise RuntimeError("Codex app-server closed during WebSocket upgrade")
            if line in {b"\r\n", b"\n"}:
                break
            name, _, value = line.decode("ascii", errors="replace").partition(":")
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if (
            not status.startswith("HTTP/1.1 101 ")
            or headers.get("sec-websocket-accept") != expected
        ):
            raise RuntimeError(f"Codex app-server rejected WebSocket upgrade: {status}")
        return used

    def _consume_bounded_response_bytes(self, used: int) -> None:
        remaining = self._bounded_response_bytes_remaining
        if remaining is None:
            return
        remaining -= used
        self._bounded_response_bytes_remaining = remaining
        if remaining < 0:
            raise _StopFailure

    def _read_line(self, max_bytes: int | None, deadline: float | None) -> bytes:
        if deadline is None:
            if max_bytes is None:
                return self.reader.readline()
            return self.reader.readline(max_bytes + 1)
        value = bytearray()
        while not value.endswith(b"\n"):
            if max_bytes is not None and len(value) >= max_bytes:
                raise _StopFailure
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.socket.settimeout(max(0.001, remaining))
            chunk = self.reader.read1(1)
            if not chunk:
                raise RuntimeError("Codex app-server closed during WebSocket upgrade")
            value.extend(chunk)
        return bytes(value)

    def _read_exact(self, length: int, deadline: float | None = None) -> bytes:
        if deadline is None:
            value = self.reader.read(length)
            if value is None or len(value) != length:
                raise RuntimeError("Codex app-server closed the WebSocket")
            return value
        value = bytearray()
        while len(value) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.socket.settimeout(max(0.001, remaining))
            chunk = self.reader.read1(length - len(value))
            if not chunk:
                raise RuntimeError("Codex app-server closed the WebSocket")
            value.extend(chunk)
        return bytes(value)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def _read_text(
        self, max_bytes: int = _MAX_MESSAGE_BYTES, deadline: float | None = None
    ) -> str:
        fragments = bytearray()
        message_opcode: int | None = None
        while True:
            first, second = self._read_exact(2, deadline)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2, deadline))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8, deadline))[0]
            if length > max_bytes:
                raise _StopFailure
            mask = self._read_exact(4, deadline) if masked else b""
            payload = self._read_exact(length, deadline)
            if masked:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x8:
                raise RuntimeError("Codex app-server closed the WebSocket")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in {0x1, 0x2}:
                message_opcode = opcode
                fragments = bytearray(payload)
            elif opcode == 0x0 and message_opcode is not None:
                fragments.extend(payload)
            else:
                continue
            if len(fragments) > max_bytes:
                fragments.clear()
                raise _StopFailure
            if final:
                if message_opcode != 0x1:
                    fragments.clear()
                    message_opcode = None
                    continue
                return bytes(fragments).decode("utf-8")

    def bounded_request(self, method: str, params: Mapping[str, Any], *,
        max_response_bytes: int = 256 * 1024, timeout_seconds: float = 3.0,
        _close_after: bool = True,
    ) -> tuple[str, Mapping[str, Any] | None]:
        """Send one bounded one-shot request, discard unrelated input, and close."""
        classification, result = "ambiguous", None
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            deadline = self._bounded_deadline
            if deadline is None:
                deadline = time.monotonic() + timeout_seconds
            try:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise TimeoutError
                self.socket.settimeout(remaining_time)
                request = {"method": method, "id": request_id, "params": dict(params)}
                self._send_frame(0x1, _canonical(request).encode("utf-8"))
                while time.monotonic() < deadline:
                    self.socket.settimeout(max(0.001, deadline - time.monotonic()))
                    response_limit = max_response_bytes
                    if self._bounded_response_bytes_remaining is not None:
                        response_limit = min(
                            response_limit, self._bounded_response_bytes_remaining
                        )
                    raw = self._read_text(response_limit, deadline)
                    self._consume_bounded_response_bytes(len(raw.encode("utf-8")))
                    try:
                        message = json.loads(raw)
                    except (UnicodeError, json.JSONDecodeError):
                        raw = ""
                        classification = "malformed"
                        break
                    raw = ""
                    if not isinstance(message, Mapping):
                        message = None
                        classification = "malformed"
                        break
                    if type(message.get("id")) is not int or message.get("id") != request_id:
                        message = None
                        continue
                    if "error" in message and "result" in message:
                        classification = "malformed"
                    elif "error" in message:
                        classification = "rejected"
                    elif isinstance(message.get("result"), Mapping):
                        classification, result = "ok", dict(message["result"])
                    else:
                        classification = "malformed"
                    message = None
                    break
            except _StopFailure: classification = "oversize"
            except UnicodeError: classification = "malformed"
            except (OSError, RuntimeError, TimeoutError): classification = "ambiguous"
            finally:
                if _close_after or classification != "ok":
                    self._stop_close()
        return classification, result

    def stop_request(self, method: str, params: Mapping[str, Any], *,
        max_response_bytes: int = 256 * 1024, timeout_seconds: float = 3.0,
        _close_after: bool = True,
    ) -> tuple[str, Mapping[str, Any] | None]:
        """Compatibility entry point for the B2 exact-turn Stop boundary."""
        return self.bounded_request(
            method,
            params,
            max_response_bytes=max_response_bytes,
            timeout_seconds=timeout_seconds,
            _close_after=_close_after,
        )

    def bounded_thread_read(
        self,
        thread_id: str,
        *,
        max_response_bytes: int = 256 * 1024,
        timeout_seconds: float = 3.0,
    ) -> tuple[str, Mapping[str, Any] | None]:
        """Read one exact thread through the bounded one-shot path."""
        return self.bounded_request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": True},
            max_response_bytes=max_response_bytes,
            timeout_seconds=timeout_seconds,
        )

    def bounded_turn_start_text_native(
        self,
        thread_id: str,
        text: str,
        *,
        max_response_bytes: int = 256 * 1024,
        timeout_seconds: float = 3.0,
    ) -> tuple[str, Mapping[str, Any] | None]:
        """Start one turn without overriding the native thread settings."""
        return self.bounded_request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": text}]},
            max_response_bytes=max_response_bytes,
            timeout_seconds=timeout_seconds,
        )

    def bounded_turn_steer_text(
        self,
        thread_id: str,
        expected_turn_id: str,
        text: str,
        *,
        max_response_bytes: int = 256 * 1024,
        timeout_seconds: float = 3.0,
    ) -> tuple[str, Mapping[str, Any] | None]:
        """Steer one exact active turn through the bounded one-shot path."""
        return self.bounded_request(
            "turn/steer",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "expectedTurnId": expected_turn_id,
            },
            max_response_bytes=max_response_bytes,
            timeout_seconds=timeout_seconds,
        )

    def _stop_close(self) -> None:
        self._server_messages.clear()
        try: self.close()
        except (OSError, ValueError): self.socket.close()

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.reader.close()
        self.socket.close()

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        with self._lock:
            self._send_frame(
                0x1, _canonical({"method": method, "params": dict(params)}).encode("utf-8")
            )

    def _request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._send_frame(
                0x1,
                _canonical({"method": method, "id": request_id, "params": dict(params)}).encode(
                    "utf-8"
                ),
            )
            while True:
                message = json.loads(self._read_text())
                if message.get("id") != request_id:
                    if isinstance(message, Mapping) and isinstance(message.get("method"), str):
                        self._server_messages.append(dict(message))
                    continue
                if "error" in message:
                    error = message.get("error") or {}
                    raise RuntimeError(
                        f"Codex app-server {method} failed: "
                        f"{error.get('code')} {error.get('message')}"
                    )
                result = message.get("result")
                if not isinstance(result, Mapping):
                    raise RuntimeError(f"Codex app-server {method} returned invalid result")
                return dict(result)

    def next_server_message(self, *, timeout_seconds: float | None = None) -> Mapping[str, Any]:
        """Return the next notification/server request, optionally with a bounded wait.

        A timeout is intended as the final read on a connection. Python's buffered
        socket reader may not be reusable after its underlying socket times out.
        """
        with self._lock:
            if self._server_messages:
                return self._server_messages.popleft()
            previous_timeout = self.socket.gettimeout()
            self.socket.settimeout(timeout_seconds)
            try:
                while True:
                    try:
                        message = json.loads(self._read_text())
                    except socket.timeout as exc:
                        raise TimeoutError("timed out waiting for an App Server message") from exc
                    if isinstance(message, Mapping) and isinstance(message.get("method"), str):
                        return dict(message)
            finally:
                self.socket.settimeout(previous_timeout)

    def drain_server_messages(self) -> list[dict[str, Any]]:
        """Return messages already observed while completing earlier requests."""
        with self._lock:
            messages = list(self._server_messages)
            self._server_messages.clear()
            return messages

    def thread_read(self, thread_id: str, *, include_turns: bool = True) -> Mapping[str, Any]:
        return self._request("thread/read", {"threadId": thread_id, "includeTurns": include_turns})

    def thread_list(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._request("thread/list", params)

    def thread_start(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._request("thread/start", params)

    def thread_name_set(self, thread_id: str, name: str) -> Mapping[str, Any]:
        return self._request("thread/name/set", {"threadId": thread_id, "name": name})

    def thread_resume(self, thread_id: str) -> Mapping[str, Any]:
        return self._request("thread/resume", {"threadId": thread_id})

    def turn_start_text(
        self,
        thread_id: str,
        text: str,
        *,
        approval_policy: str = "never",
        sandbox_policy: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        return self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "approvalPolicy": approval_policy,
                "sandboxPolicy": dict(sandbox_policy or {"type": "workspaceWrite"}),
            },
        )

    def turn_start_text_native(self, thread_id: str, text: str) -> Mapping[str, Any]:
        """Start input without overriding the selected native thread's settings."""
        return self._request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": text}]},
        )

    def turn_steer_text(
        self, thread_id: str, expected_turn_id: str, text: str
    ) -> Mapping[str, Any]:
        return self._request(
            "turn/steer",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "expectedTurnId": expected_turn_id,
            },
        )

    def turn_interrupt(self, thread_id: str, turn_id: str) -> Mapping[str, Any]:
        return self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
