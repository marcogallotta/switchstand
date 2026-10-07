from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from switchstand.codex_app_server import (
    SOCKET_PLACEHOLDER,
    THREAD_PLACEHOLDER,
    open_private_append,
    prepare_socket,
    remote_command,
    start_app_server,
    stop_process,
)


def _executable(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o700)
    return path


def test_private_log_requires_owned_mode_0600_file(tmp_path: Path) -> None:
    path = tmp_path / "wakeful.log"
    descriptor = open_private_append(path)
    os.close(descriptor)
    assert path.stat().st_mode & 0o777 == 0o600

    path.chmod(0o644)
    with pytest.raises(ValueError, match="mode-0600"):
        open_private_append(path)


def test_prepare_socket_removes_only_stale_owned_socket() -> None:
    with tempfile.TemporaryDirectory(prefix="wf-", dir=Path.home() / ".cache") as temporary:
        home = Path(temporary)
        socket_path = home / "app-server.sock"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(socket_path))
        stale.close()

        prepare_socket(home, socket_path)
        assert not socket_path.exists()

        active = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        active.bind(str(socket_path))
        active.listen()
        try:
            with pytest.raises(ValueError, match="already active"):
                prepare_socket(home, socket_path)
        finally:
            active.close()
            socket_path.unlink()


def test_remote_command_substitutes_exact_thread_and_socket(tmp_path: Path) -> None:
    socket_path = tmp_path / "app-server.sock"
    assert remote_command(
        ["codex", "resume", THREAD_PLACEHOLDER, "--remote", SOCKET_PLACEHOLDER],
        "thread-1",
        socket_path,
    ) == ["codex", "resume", "thread-1", "--remote", f"unix://{socket_path}"]

    with pytest.raises(ValueError, match="exact Wakeful placeholders"):
        remote_command(["codex"], "thread-1", socket_path)


def test_start_app_server_waits_for_private_socket() -> None:
    with tempfile.TemporaryDirectory(prefix="wf-", dir=Path.home() / ".cache") as temporary:
        home = Path(temporary)
        socket_path = home / "app-server.sock"
        log_path = home / "app-server.log"
        codex = _executable(home / "codex", """\
import socket
import sys
import time

path = sys.argv[-1].removeprefix("unix://")
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(path)
listener.listen()
time.sleep(10)
""")
        process = start_app_server(
            codex, home, socket_path, log_path, dict(os.environ), timeout=2,
        )
        try:
            assert socket_path.stat().st_mode & 0o777 == 0o600
            assert process.poll() is None
        finally:
            stop_process(process, 1)


def test_start_app_server_reports_child_exit() -> None:
    with tempfile.TemporaryDirectory(prefix="wf-", dir=Path.home() / ".cache") as temporary:
        home = Path(temporary)
        codex = _executable(home / "codex", "raise SystemExit(9)\n")
        with pytest.raises(OSError, match=r"exited before readiness \(status 9\)"):
            start_app_server(
                codex, home, home / "app-server.sock", home / "app-server.log",
                dict(os.environ), timeout=1,
            )


def test_start_app_server_timeout_stops_child() -> None:
    with tempfile.TemporaryDirectory(prefix="wf-", dir=Path.home() / ".cache") as temporary:
        home = Path(temporary)
        pid_path = home / "child.pid"
        codex = _executable(home / "codex", f"""\
import os
import time
from pathlib import Path

Path({str(pid_path)!r}).write_text(str(os.getpid()))
time.sleep(10)
""")
        with pytest.raises(OSError, match="readiness timed out"):
            start_app_server(
                codex, home, home / "app-server.sock", home / "app-server.log",
                dict(os.environ), timeout=0.2, term_seconds=0.2,
            )
        pid = int(pid_path.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
