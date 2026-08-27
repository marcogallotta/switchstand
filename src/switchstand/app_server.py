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


class _MessageTooLarge(RuntimeError): pass


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
    ) -> None:
        self.socket_path = Path(socket_path)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout_seconds)
        self.socket.connect(str(self.socket_path))
        self.reader = self.socket.makefile("rb")
        self._next_id = 0
        self._lock = threading.Lock()
        self._server_messages: deque[dict[str, Any]] = deque()
        self._upgrade()
        self._request(
            "initialize",
            {
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
            },
        )
        self._notify("initialized", {})

    def _upgrade(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.socket.sendall(request)
        status = self.reader.readline().decode("ascii", errors="replace").strip()
        headers: dict[str, str] = {}
        while True:
            line = self.reader.readline()
            if not line:
                raise RuntimeError("Codex app-server closed during WebSocket upgrade")
            if line in {b"\r\n", b"\n"}:
                break
            name, _, value = line.decode("ascii", errors="replace").partition(":")
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if not status.startswith("HTTP/1.1 101 ") or headers.get("sec-websocket-accept") != expected:
            raise RuntimeError(f"Codex app-server rejected WebSocket upgrade: {status}")

    def _read_exact(self, length: int) -> bytes:
        value = self.reader.read(length)
        if value is None or len(value) != length:
            raise RuntimeError("Codex app-server closed the WebSocket")
        return value

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

    def _read_text(self, max_bytes: int = _MAX_MESSAGE_BYTES) -> str:
        fragments = bytearray()
        message_opcode: int | None = None
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if length > max_bytes:
                raise _MessageTooLarge
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
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
                raise _MessageTooLarge
            if final:
                if message_opcode != 0x1:
                    fragments.clear()
                    message_opcode = None
                    continue
                return bytes(fragments).decode("utf-8")

    def stop_request(self, method: str, params: Mapping[str, Any], *,
        max_response_bytes: int = 256 * 1024, timeout_seconds: float = 3.0,
    ) -> tuple[str, Mapping[str, Any] | None]:
        """Send one bounded B2 request, discard all unrelated input, and close."""
        classification = "ambiguous"
        result: Mapping[str, Any] | None = None
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            deadline = time.monotonic() + timeout_seconds
            try:
                self.socket.settimeout(timeout_seconds)
                request = {"method": method, "id": request_id, "params": dict(params)}
                self._send_frame(0x1, _canonical(request).encode("utf-8"))
                while time.monotonic() < deadline:
                    self.socket.settimeout(max(0.001, deadline - time.monotonic()))
                    raw = self._read_text(max_response_bytes)
                    try:
                        message = json.loads(raw)
                    except (UnicodeError, json.JSONDecodeError):
                        raw = ""
                        classification = "malformed"
                        break
                    raw = ""
                    if not isinstance(message, Mapping) or message.get("id") != request_id:
                        message = None
                        continue
                    if "error" in message:
                        classification = "rejected"
                    elif isinstance(message.get("result"), Mapping):
                        classification, result = "ok", dict(message["result"])
                    else:
                        classification = "malformed"
                    message = None
                    break
            except _MessageTooLarge:
                classification = "oversize"
            except UnicodeError:
                classification = "malformed"
            except (OSError, RuntimeError, TimeoutError):
                classification = "ambiguous"
            finally:
                self._server_messages.clear()
                try:
                    self.close()
                except (OSError, ValueError):
                    pass
        return classification, result

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
