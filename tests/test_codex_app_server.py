from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

import pytest

from switchstand.codex_app_server import (
    SOCKET_PLACEHOLDER,
    THREAD_PLACEHOLDER,
    open_private_append,
    prepare_socket,
    remote_command,
)


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
