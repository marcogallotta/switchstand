"""Deadline-aware legacy App Server sessions.

This is deliberately separate from the native bounded transport. It applies one
caller-owned absolute cutoff to setup, target requests, and graceful close while
always forcing local descriptor cleanup.
"""
from __future__ import annotations

import base64
from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import threading
from typing import Any, Mapping

from .app_server import CodexAppServer, _MAX_MESSAGE_BYTES, _canonical
from .legacy_deadline import LegacyDeadline, NotificationDisposition, PhaseResult, SetupResult


_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_UPGRADE_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 100


class _LegacyNotSent(RuntimeError):
    pass


class _MalformedLegacyResponse(ValueError):
    pass


def _reject_json_constant(value: str) -> None:
    raise _MalformedLegacyResponse(f"invalid JSON constant: {value}")


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _MalformedLegacyResponse("non-finite JSON number")
    return parsed


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _MalformedLegacyResponse("duplicate JSON object key")
        value[key] = item
    return value


def _strict_json_message(raw: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            raw,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
            object_pairs_hook=_json_object,
        )
        if not isinstance(value, Mapping):
            raise _MalformedLegacyResponse("response is not an object")
        pending: list[tuple[Any, int]] = [(value, 1)]
        while pending:
            item, depth = pending.pop()
            if depth > _MAX_JSON_DEPTH:
                raise _MalformedLegacyResponse("response nesting is too deep")
            if isinstance(item, Mapping):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
            elif type(item) is float and not math.isfinite(item):
                raise _MalformedLegacyResponse("non-finite JSON number")
        return value
    except _MalformedLegacyResponse:
        raise
    except Exception as exc:
        raise _MalformedLegacyResponse("JSON decoding failed") from exc


def _valid_server_message(message: Mapping[str, Any]) -> bool:
    return (
        set(message) == {"method", "params"}
        and type(message.get("method")) is str
        and bool(message.get("method"))
        and isinstance(message.get("params"), Mapping)
    )


def _upgrade_legacy(client: CodexAppServer, deadline: LegacyDeadline) -> None:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET / HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    remaining = deadline.remaining()
    if remaining <= 0.0:
        raise _LegacyNotSent
    client.socket.settimeout(remaining)
    if deadline.expired():
        raise _LegacyNotSent
    client.socket.sendall(request)
    status_line = client._read_line(_MAX_UPGRADE_BYTES, deadline.cutoff)
    used = len(status_line)
    status = status_line.decode("ascii").strip()
    headers: dict[str, str] = {}
    while True:
        line = client._read_line(_MAX_UPGRADE_BYTES - used, deadline.cutoff)
        used += len(line)
        if used > _MAX_UPGRADE_BYTES:
            raise RuntimeError("oversize WebSocket upgrade")
        if not line:
            raise RuntimeError("closed during WebSocket upgrade")
        if line in {b"\r\n", b"\n"}:
            break
        name, separator, value = line.decode("ascii").partition(":")
        if not separator:
            raise RuntimeError("malformed WebSocket upgrade")
        headers[name.strip().lower()] = value.strip()
    expected = base64.b64encode(
        hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
    ).decode("ascii")
    if (
        not status.startswith("HTTP/1.1 101 ")
        or headers.get("sec-websocket-accept") != expected
    ):
        raise RuntimeError("rejected WebSocket upgrade")


def _force_close(client: CodexAppServer) -> None:
    try:
        client._server_messages.clear()
    except BaseException:
        pass
    try:
        client.socket.shutdown(socket.SHUT_RDWR)
    except BaseException:
        pass
    try:
        client.reader.close()
    except BaseException:
        pass
    try:
        client.socket.close()
    except BaseException:
        pass


def close_legacy(client: CodexAppServer, deadline: LegacyDeadline) -> str:
    """Admit at most one graceful close frame, then force local cleanup."""
    disposition = "not_sent"
    try:
        try:
            remaining = deadline.remaining()
            if remaining <= 0.0:
                return disposition
            client.socket.settimeout(remaining)
            if deadline.expired():
                return disposition
            client._send_frame(0x8, b"")
            disposition = "sent"
        except BaseException:
            disposition = "ambiguous"
    finally:
        _force_close(client)
    return disposition


def request_legacy(
    client: CodexAppServer,
    method: str,
    params: Mapping[str, Any],
    deadline: LegacyDeadline,
) -> PhaseResult:
    """Issue one exact request without retrying or reconnecting."""
    if deadline.expired():
        return PhaseResult("not_sent", method, code="setup_cutoff")
    with client._lock:
        client._next_id += 1
        request_id = client._next_id
        payload = _canonical({"method": method, "id": request_id, "params": dict(params)}).encode("utf-8")
        remaining = deadline.remaining()
        if remaining <= 0.0:
            return PhaseResult("not_sent", method, code="setup_cutoff")
        try:
            client.socket.settimeout(remaining)
            if deadline.expired():
                return PhaseResult("not_sent", method, code="setup_cutoff")
            client._send_frame(0x1, payload)
            while True:
                remaining = deadline.remaining()
                if remaining <= 0.0:
                    raise TimeoutError
                client.socket.settimeout(remaining)
                raw = client._read_text(_MAX_MESSAGE_BYTES, deadline.cutoff)
                try:
                    message = _strict_json_message(raw)
                except _MalformedLegacyResponse:
                    return PhaseResult("ambiguous", method, code="malformed_response")
                if "id" not in message:
                    if _valid_server_message(message):
                        client._server_messages.append(dict(message))
                        continue
                    return PhaseResult("ambiguous", method, code="malformed_response")
                if type(message.get("id")) is not int:
                    return PhaseResult("ambiguous", method, code="malformed_response")
                if message.get("id") != request_id:
                    return PhaseResult("ambiguous", method, code="malformed_response")
                keys = set(message)
                if keys == {"id", "error"}:
                    error = message.get("error")
                    if not (
                        isinstance(error, Mapping)
                        and set(error).issubset({"code", "message", "data"})
                        and {"code", "message"}.issubset(error)
                        and type(error.get("code")) is int
                        and type(error.get("message")) is str
                    ):
                        return PhaseResult("ambiguous", method, code="malformed_response")
                    return PhaseResult("rejected", method, code="app_server_rejected")
                if keys != {"id", "result"}:
                    return PhaseResult("ambiguous", method, code="malformed_response")
                result = message.get("result")
                if not isinstance(result, Mapping):
                    return PhaseResult("ambiguous", method, code="missing_exact_acknowledgement")
                return PhaseResult("acknowledged", method, dict(result))
        except (OSError, RuntimeError, TimeoutError, UnicodeError):
            return PhaseResult("ambiguous", method, code="acknowledgement_unavailable")


def _notify_initialized(client: CodexAppServer, deadline: LegacyDeadline) -> NotificationDisposition:
    payload = _canonical({"method": "initialized", "params": {}}).encode("utf-8")
    remaining = deadline.remaining()
    if remaining <= 0.0:
        return "not_sent"
    try:
        client.socket.settimeout(remaining)
        if deadline.expired():
            return "not_sent"
        client._send_frame(0x1, payload)
        return "sent"
    except (OSError, RuntimeError, TimeoutError):
        return "ambiguous"


def open_legacy(socket_path: Path | str, deadline: LegacyDeadline) -> SetupResult:
    """Open and initialize one legacy connection under the outer cutoff."""
    if deadline.expired():
        return SetupResult(None, PhaseResult("not_sent", "connect", code="setup_cutoff"), "not_sent")
    client = object.__new__(CodexAppServer)
    client._bounded_deadline = None
    client._bounded_response_bytes_remaining = None
    client.socket_path = Path(socket_path)
    try:
        client.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return SetupResult(None, PhaseResult("ambiguous", "setup", code="setup_ambiguous"), "not_sent")
    client._next_id = 0
    client._lock = threading.Lock()
    client._server_messages = deque()
    try:
        remaining = deadline.remaining()
        if remaining <= 0.0:
            _force_close_socket_only(client)
            return SetupResult(None, PhaseResult("not_sent", "connect", code="setup_cutoff"), "not_sent")
        client.socket.settimeout(remaining)
        if deadline.expired():
            _force_close_socket_only(client)
            return SetupResult(None, PhaseResult("not_sent", "connect", code="setup_cutoff"), "not_sent")
        client.socket.connect(str(client.socket_path))
        if deadline.expired():
            _force_close_socket_only(client)
            return SetupResult(None, PhaseResult("not_sent", "setup", code="setup_cutoff"), "not_sent")
        client.reader = client.socket.makefile("rb")
        _upgrade_legacy(client, deadline)
    except _LegacyNotSent:
        _force_close_partial(client)
        return SetupResult(None, PhaseResult("not_sent", "setup", code="setup_cutoff"), "not_sent")
    except (OSError, RuntimeError, TimeoutError, UnicodeError):
        _force_close_partial(client)
        return SetupResult(None, PhaseResult("ambiguous", "setup", code="setup_ambiguous"), "not_sent")

    initialize = request_legacy(
        client,
        "initialize",
        {
            "clientInfo": {"name": "switchstand", "title": "Switchstand", "version": "1"},
            "capabilities": {
                "experimentalApi": True,
                "optOutNotificationMethods": [
                    "item/agentMessage/delta",
                    "item/reasoning/summaryTextDelta",
                    "thread/tokenUsage/updated",
                ],
            },
        },
        deadline,
    )
    if initialize.disposition != "acknowledged":
        close_legacy(client, deadline)
        code = "setup_rejected" if initialize.disposition == "rejected" else (
            "setup_cutoff" if initialize.disposition == "not_sent" else "setup_ambiguous"
        )
        return SetupResult(None, PhaseResult(initialize.disposition, "initialize", code=code), "not_sent")
    initialized = _notify_initialized(client, deadline)
    if initialized != "sent":
        close_legacy(client, deadline)
        disposition = "not_sent" if initialized == "not_sent" else "ambiguous"
        code = "setup_cutoff" if initialized == "not_sent" else "setup_ambiguous"
        return SetupResult(None, PhaseResult(disposition, "initialized", code=code), initialized)
    return SetupResult(client, PhaseResult("acknowledged", "setup", initialize.result), "sent")


def _force_close_socket_only(client: CodexAppServer) -> None:
    try:
        client.socket.close()
    except BaseException:
        pass


def _force_close_partial(client: CodexAppServer) -> None:
    if hasattr(client, "reader"):
        _force_close(client)
    else:
        _force_close_socket_only(client)
