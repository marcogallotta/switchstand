"""Private launch-owned Codex app-server lifecycle primitives."""
from __future__ import annotations

import errno
import json
import os
import signal
import socket
import stat
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

THREAD_PLACEHOLDER = "__SWITCHSTAND_WAKEFUL_THREAD__"
SOCKET_PLACEHOLDER = "__SWITCHSTAND_WAKEFUL_SOCKET__"


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ",".join(_toml(item) for item in cast(list[object], value)) + "]"
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        return "{" + ",".join(
            f"{json.dumps(key)}={_toml(item)}"
            for key, item in sorted(mapping.items())
        ) + "}"
    raise TypeError(f"unsupported hook value: {type(value).__name__}")


def open_private_append(path: Path) -> int:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise ValueError("Wakeful diagnostic log must be an owned mode-0600 regular file")
    return descriptor


def prepare_socket(home: Path, socket_path: Path) -> None:
    metadata = home.lstat()
    if (
        home.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("Codex home must be an owned mode-0700 directory")
    try:
        socket_metadata = socket_path.lstat()
    except FileNotFoundError:
        return
    if (
        socket_metadata.st_uid != os.getuid()
        or not (stat.S_ISSOCK(socket_metadata.st_mode) or stat.S_ISLNK(socket_metadata.st_mode))
    ):
        raise ValueError("launch-owned app-server path is not an owned socket entry")
    try:
        target_metadata = socket_path.stat()
    except FileNotFoundError:
        socket_path.unlink()
        return
    if not stat.S_ISSOCK(target_metadata.st_mode) or target_metadata.st_uid != os.getuid():
        raise ValueError("launch-owned app-server target is not an owned socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        result = probe.connect_ex(str(socket_path))
        if result == 0:
            raise ValueError("launch-owned app-server is already active")
        if result not in {errno.ECONNREFUSED, errno.ENOENT}:
            raise OSError(result, "app-server socket liveness is unknown")
    finally:
        probe.close()
    socket_path.unlink()


def socket_ready(socket_path: Path) -> bool:
    """Return true only when an owned socket endpoint accepts a connection."""
    try:
        entry_metadata = socket_path.lstat()
    except FileNotFoundError:
        return False
    if (
        entry_metadata.st_uid != os.getuid()
        or not (stat.S_ISSOCK(entry_metadata.st_mode) or stat.S_ISLNK(entry_metadata.st_mode))
    ):
        raise ValueError("launch-owned app-server path is not an owned socket entry")
    try:
        target_metadata = socket_path.stat()
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(target_metadata.st_mode) or target_metadata.st_uid != os.getuid():
        raise ValueError("launch-owned app-server target is not an owned socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        result = probe.connect_ex(str(socket_path))
    finally:
        probe.close()
    if result == 0:
        socket_path.chmod(0o600)
        return True
    if result in {errno.ECONNREFUSED, errno.ENOENT}:
        return False
    raise OSError(result, "app-server socket readiness is unknown")


def remove_owned_socket_entry(socket_path: Path) -> None:
    """Remove only an owned socket or symlink at the exact launch-owned path."""
    try:
        metadata = socket_path.lstat()
    except FileNotFoundError:
        return
    if metadata.st_uid == os.getuid() and (
        stat.S_ISSOCK(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
    ):
        socket_path.unlink()


def start_app_server(
    codex: Path,
    home: Path,
    hooks: Mapping[str, object],
    socket_path: Path,
    log_path: Path,
    environment: dict[str, str],
    *,
    timeout: float = 10.0,
    term_seconds: float = 2.0,
) -> subprocess.Popen[bytes]:
    """Start one private app-server and return only after its socket is ready."""
    prepare_socket(home, socket_path)
    log_descriptor = open_private_append(log_path)
    try:
        process = subprocess.Popen(
            [
                str(codex),
                "-c",
                f"hooks={_toml(hooks)}",
                "--enable",
                "hooks",
                "--dangerously-bypass-hook-trust",
                "app-server",
                "--listen",
                f"unix://{socket_path}",
            ],
            env=environment,
            start_new_session=True,
            stdout=log_descriptor,
            stderr=log_descriptor,
        )
    finally:
        os.close(log_descriptor)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise OSError(
                    f"launch-owned app-server exited before readiness (status {process.returncode})"
                )
            if socket_ready(socket_path):
                return process
            time.sleep(0.05)
        raise OSError("launch-owned app-server readiness timed out")
    except Exception:
        stop_process(process, term_seconds)
        raise


def stop_process(process: subprocess.Popen[bytes], timeout: float) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def remote_command(command: list[str], thread_id: str, socket_path: Path) -> list[str]:
    if command.count(THREAD_PLACEHOLDER) != 1 or command.count(SOCKET_PLACEHOLDER) != 1:
        raise ValueError("Codex command does not contain exact Wakeful placeholders")
    return [
        thread_id if value == THREAD_PLACEHOLDER
        else f"unix://{socket_path}" if value == SOCKET_PLACEHOLDER
        else value
        for value in command
    ]
