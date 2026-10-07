from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from switchstand.codex_session import (
    SOCKET_PLACEHOLDER,
    THREAD_PLACEHOLDER,
    SessionSpec,
    _database_url,
    _prepare_socket,
    _start_runner,
    _stop_runner,
    main,
    prepare_runner,
    supervise,
)
from switchstand.codex_wakeful import CodexBinding


def private_environment(path: Path) -> Path:
    path.write_text("IGNORED=value\nDATABASE_URL='postgresql://exact'\n")
    path.chmod(0o600)
    return path


def spec(tmp_path: Path) -> SessionSpec:
    codex = tmp_path / "codex"
    codex.write_text("")
    codex.chmod(0o700)
    start = tmp_path / "start-commit.exact"
    start.write_text("sha\n")
    start.chmod(0o600)
    profile = tmp_path / "profile.toml"
    profile.write_text(f'developer_instructions = "use {start}"\n')
    return SessionSpec(
        home=tmp_path,
        codex=codex,
        start_record=start,
        default_name="codex-head-exact",
        environment_file=private_environment(tmp_path / ".env"),
        profile_path=profile,
    )


def test_database_url_reads_only_private_owned_environment(tmp_path: Path) -> None:
    environment = private_environment(tmp_path / ".env")
    assert _database_url(environment) == "postgresql://exact"
    environment.chmod(0o644)
    with pytest.raises(ValueError, match="mode-0600"):
        _database_url(environment)


def test_prepare_socket_removes_only_stale_owned_socket() -> None:
    with tempfile.TemporaryDirectory(prefix="wf-", dir=Path.home() / ".cache") as temporary:
        session = spec(Path(temporary))
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(session.socket_path))
        stale.close()

        _prepare_socket(session)
        assert not session.socket_path.exists()

        active = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        active.bind(str(session.socket_path))
        active.listen()
        try:
            with pytest.raises(ValueError, match="already active"):
                _prepare_socket(session)
        finally:
            active.close()
            session.socket_path.unlink()


def test_prepare_creates_and_registers_exact_launch_owned_thread(
    tmp_path: Path, monkeypatch,
) -> None:
    session = spec(tmp_path)
    binding = CodexBinding("thread-1", str(session.start_record), session.start_record.name)
    calls: list[tuple[str, dict[str, object]]] = []

    class Client:
        def __init__(self, codex: Path, home: Path, socket_path: Path):
            assert (codex, home) == (session.codex, session.home)
            assert socket_path == session.socket_path

        def call(self, method: str, arguments: dict[str, object]):
            calls.append((method, arguments))
            if method == "thread/start":
                return {"thread": {"id": "thread-1"}}
            return {"structuredContent": {"status": "ok", "name": "codex-head-exact"}}

        def close(self) -> None:
            calls.append(("closed", {}))

    observed: list[tuple[CodexBinding, str, str]] = []

    async def freeze(
        value: SessionSpec, exact: CodexBinding, name: str, database_url: str,
    ) -> Path:
        assert value == session
        observed.append((exact, name, database_url))
        return tmp_path / "runner.json"

    monkeypatch.setattr("switchstand.codex_session.QueueClient", Client)
    monkeypatch.setattr("switchstand.codex_session.bind", lambda *_args: binding)
    monkeypatch.setattr("switchstand.codex_session._freeze_config", freeze)
    async def existing(*_args):
        return "missing", None
    monkeypatch.setattr("switchstand.codex_session._existing_live_registration", existing)
    assert prepare_runner(session) == (
        tmp_path / "runner.json", "postgresql://exact", binding,
    )
    assert calls[0][0] == "thread/start"
    assert calls[1] == ("mcpServer/tool/call", {
        "threadId": "thread-1",
        "server": "switchstand",
        "tool": "agent_register",
        "arguments": {"api_version": "1", "name": "codex-head-exact"},
    })
    assert observed == [(binding, "codex-head-exact", "postgresql://exact")]
    assert (tmp_path / "wakeful-thread-exact").read_text() == "thread-1\n"


def test_prepare_reconciles_committed_live_registration_before_retrying(
    tmp_path: Path, monkeypatch,
) -> None:
    session = spec(tmp_path)
    binding = CodexBinding("thread-1", str(session.start_record), session.start_record.name)
    calls: list[str] = []

    class Client:
        def __init__(self, _codex: Path, _home: Path, _socket_path: Path):
            pass

        def call(self, method: str, _arguments: dict[str, object]):
            calls.append(method)
            raise AssertionError("helper registration must not be retried")

        def close(self) -> None:
            calls.append("closed")

    async def existing(*_args):
        return "exact", "codex-head-exact"

    async def freeze(*_args):
        return tmp_path / "runner.json"

    monkeypatch.setattr("switchstand.codex_session.QueueClient", Client)
    monkeypatch.setattr("switchstand.codex_session.bind", lambda *_args: binding)
    monkeypatch.setattr("switchstand.codex_session._existing_live_registration", existing)
    monkeypatch.setattr("switchstand.codex_session._freeze_config", freeze)
    thread = tmp_path / "wakeful-thread-exact"
    thread.write_text("thread-1\n")
    thread.chmod(0o600)

    assert prepare_runner(session) == (
        tmp_path / "runner.json", "postgresql://exact", binding,
    )
    assert calls == ["closed"]


def test_prepare_retires_unbound_persisted_thread_before_registration(
    tmp_path: Path, monkeypatch,
) -> None:
    session = spec(tmp_path)
    thread = tmp_path / "wakeful-thread-exact"
    thread.write_text("stale-thread\n")
    thread.chmod(0o600)
    calls: list[str] = []

    class Client:
        def __init__(self, *_args):
            pass

        def call(self, method: str, _arguments: dict[str, object]):
            calls.append(method)
            if method == "thread/start":
                return {"thread": {"id": "fresh-thread"}}
            return {"structuredContent": {"status": "ok", "name": session.default_name}}

        def close(self) -> None:
            pass

    fresh = CodexBinding("fresh-thread", str(session.start_record), session.start_record.name)
    monkeypatch.setattr("switchstand.codex_session.QueueClient", Client)
    monkeypatch.setattr(
        "switchstand.codex_session.bind",
        lambda _client, _home, _record, thread_id: (
            fresh if thread_id == "fresh-thread" else "NOT_BOUND"
        ),
    )
    monkeypatch.setattr(
        "switchstand.codex_session._existing_live_registration",
        lambda *_args: _async_value(("missing", None)),
    )
    monkeypatch.setattr(
        "switchstand.codex_session._freeze_config",
        lambda *_args: _async_value(tmp_path / "runner.json"),
    )

    assert prepare_runner(session)[2] == fresh
    assert thread.read_text() == "fresh-thread\n"
    assert calls == ["thread/start", "mcpServer/tool/call"]


async def _async_value(value):
    return value


def test_prepare_never_automatically_takes_over_root(tmp_path: Path, monkeypatch) -> None:
    session = replace(spec(tmp_path), default_name="/root")
    binding = CodexBinding("thread-1", str(session.start_record), session.start_record.name)

    class Client:
        def __init__(self, *_args):
            pass

        def call(self, method: str, _arguments: dict[str, object]):
            if method == "thread/start":
                return {"thread": {"id": "thread-1"}}
            raise AssertionError("root takeover must require explicit authorization")

        def close(self) -> None:
            pass

    monkeypatch.setattr("switchstand.codex_session.QueueClient", Client)
    monkeypatch.setattr("switchstand.codex_session.bind", lambda *_args: binding)
    monkeypatch.setattr(
        "switchstand.codex_session._existing_live_registration",
        lambda *_args: _async_value(("takeover", "/root")),
    )

    with pytest.raises(ValueError, match="explicit takeover"):
        prepare_runner(session)


def test_prepare_reconciles_committed_registration_after_lost_response(
    tmp_path: Path, monkeypatch,
) -> None:
    session = spec(tmp_path)
    binding = CodexBinding("thread-1", str(session.start_record), session.start_record.name)
    calls: list[str] = []
    attempt = 0

    class Client:
        def __init__(self, _codex: Path, _home: Path, _socket_path: Path):
            pass

        def call(self, method: str, _arguments: dict[str, object]):
            calls.append(method)
            if method == "thread/start":
                return {"thread": {"id": f"thread-{calls.count('thread/start')}"}}
            if calls.count("mcpServer/tool/call") == 1:
                raise OSError("response lost after durable registration")
            return {"structuredContent": {"status": "ok", "name": "codex-head-exact"}}

        def close(self) -> None:
            calls.append("closed")

    async def existing(_binding, _name, _database_url):
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            return "missing", None
        return "takeover", "codex-head-exact"

    async def freeze(*_args):
        return tmp_path / "runner.json"

    monkeypatch.setattr("switchstand.codex_session.QueueClient", Client)
    monkeypatch.setattr("switchstand.codex_session.bind", lambda *_args: binding)
    monkeypatch.setattr("switchstand.codex_session._existing_live_registration", existing)
    monkeypatch.setattr("switchstand.codex_session._freeze_config", freeze)

    with pytest.raises(OSError, match="response lost"):
        prepare_runner(session)
    assert prepare_runner(session) == (
        tmp_path / "runner.json", "postgresql://exact", binding,
    )
    assert calls.count("thread/start") == 2
    assert calls.count("mcpServer/tool/call") == 2


def test_runner_lifeline_eof_is_inherited_only_by_runner(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "eof"

    def command(_config: Path, descriptor: int) -> tuple[str, ...]:
        program = (
            "import os,sys; "
            "fd=int(sys.argv[1]); "
            "assert os.read(fd, 1) == b''; "
            "open(sys.argv[2], 'w').write('stopped')"
        )
        return sys.executable, "-c", program, str(descriptor), str(marker)

    monkeypatch.setattr("switchstand.codex_session._runner_command", command)
    runner, lifeline = _start_runner(tmp_path / "config", "postgresql://unused")
    _stop_runner(runner, lifeline)
    assert runner.returncode == 0
    assert marker.read_text() == "stopped"


def test_supervisor_restarts_runner_and_stops_it_at_codex_exit(
    tmp_path: Path, monkeypatch,
) -> None:
    session = spec(tmp_path)
    marker = tmp_path / "runner-starts"

    def command(_config: Path, descriptor: int) -> tuple[str, ...]:
        program = (
            "import os,pathlib,sys; "
            "p=pathlib.Path(sys.argv[2]); old=p.read_text() if p.exists() else ''; "
            "p.write_text(old+'x'); "
            "sys.exit(0) if not old else None; "
            "assert os.read(int(sys.argv[1]),1)==b''; "
            "p.write_text(p.read_text()+'s')"
        )
        return sys.executable, "-c", program, str(descriptor), str(marker)

    monkeypatch.setattr(
        "switchstand.codex_session.prepare_runner",
        lambda _spec: (
            tmp_path / "runner.json", "postgresql://unused",
            CodexBinding("thread-1", str(session.start_record), session.start_record.name),
        ),
    )
    monkeypatch.setattr("switchstand.codex_session._runner_command", command)
    monkeypatch.setattr(
        "switchstand.codex_session._start_app_server",
        lambda *_args: subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"], start_new_session=True,
        ),
    )

    result = supervise(
        session, [sys.executable, "-c", "import time; time.sleep(1.4); raise SystemExit(7)",
                  THREAD_PLACEHOLDER, SOCKET_PLACEHOLDER],
        dict(os.environ),
    )
    assert result == 7
    assert marker.read_text() == "xxs"


def test_supervisor_fails_closed_before_starting_unregistered_codex(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    session = spec(tmp_path)
    attempts = 0

    def unavailable(_spec: SessionSpec):
        nonlocal attempts
        attempts += 1
        raise ValueError("unavailable")

    monkeypatch.setattr("switchstand.codex_session.prepare_runner", unavailable)
    monkeypatch.setattr(
        "switchstand.codex_session._start_app_server",
        lambda *_args: subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"], start_new_session=True,
        ),
    )

    with pytest.raises(ValueError, match="unavailable"):
        supervise(session, [sys.executable, THREAD_PLACEHOLDER, SOCKET_PLACEHOLDER],
                  dict(os.environ))
    assert attempts == 1
    assert "Wakeful registration unavailable" not in capsys.readouterr().err
    diagnostic = (tmp_path / "wakeful-session-exact.log").read_text()
    assert '"stage":"registration"' in diagnostic
    assert '"error_type":"ValueError"' in diagnostic


def test_supervisor_stops_session_when_launch_owned_app_server_exits(
    tmp_path: Path, monkeypatch,
) -> None:
    session = spec(tmp_path)
    app_server = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(9)"], start_new_session=True,
    )
    monkeypatch.setattr(
        "switchstand.codex_session._start_app_server", lambda *_args: app_server,
    )
    monkeypatch.setattr(
        "switchstand.codex_session.prepare_runner",
        lambda _spec: (
            tmp_path / "runner.json", "postgresql://unused",
            CodexBinding("thread-1", str(session.start_record), session.start_record.name),
        ),
    )
    monkeypatch.setattr(
        "switchstand.codex_session._start_runner",
        lambda *_args: (_ for _ in ()).throw(OSError("runner unavailable")),
    )

    assert supervise(
        session,
        [sys.executable, "-c", "import time; time.sleep(10)",
         THREAD_PLACEHOLDER, SOCKET_PLACEHOLDER],
        dict(os.environ),
    ) == 1
    assert '"event":"app_server_exited"' in (
        tmp_path / "wakeful-session-exact.log"
    ).read_text()


@pytest.mark.parametrize("pythonpath", ["writer-specific-path", None])
def test_main_preserves_child_pythonpath(
    tmp_path: Path, monkeypatch, pythonpath: str | None,
) -> None:
    session = spec(tmp_path)
    observed: list[dict[str, str]] = []
    if pythonpath is None:
        monkeypatch.delenv("PYTHONPATH", raising=False)
    else:
        monkeypatch.setenv("PYTHONPATH", pythonpath)
    monkeypatch.setattr(
        "switchstand.codex_session.supervise",
        lambda _spec, _command, environment: observed.append(environment) or 0,
    )
    monkeypatch.setattr(sys, "argv", [
        "codex-session", "--home", str(session.home), "--codex", str(session.codex),
            "--start-record", str(session.start_record), "--default-name", session.default_name,
            "--environment-file", str(session.environment_file),
            "--profile-path", str(session.profile_path), "--", str(session.codex),
    ])
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 0
    assert observed[0].get("PYTHONPATH") == pythonpath
