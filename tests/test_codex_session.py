from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from switchstand.codex_session import (
    SessionSpec,
    _database_url,
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
    return SessionSpec(
        home=tmp_path,
        codex=codex,
        start_record=start,
        default_name="codex-head-exact",
        environment_file=private_environment(tmp_path / ".env"),
    )


def test_database_url_reads_only_private_owned_environment(tmp_path: Path) -> None:
    environment = private_environment(tmp_path / ".env")
    assert _database_url(environment) == "postgresql://exact"
    environment.chmod(0o644)
    with pytest.raises(ValueError, match="mode-0600"):
        _database_url(environment)


def test_prepare_registers_on_owned_helper_then_rebinds_exact_thread(
    tmp_path: Path, monkeypatch,
) -> None:
    session = spec(tmp_path)
    binding = CodexBinding("thread-1", str(session.start_record), session.start_record.name)
    calls: list[tuple[str, dict[str, object]]] = []

    class Client:
        def __init__(self, codex: Path, home: Path):
            assert (codex, home) == (session.codex, session.home)

        def call(self, method: str, arguments: dict[str, object]):
            calls.append((method, arguments))
            if method == "thread/start":
                return {"thread": {"id": "registration-thread"}}
            return {"structuredContent": {"status": "ok", "name": "/root"}}

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
        return None
    monkeypatch.setattr("switchstand.codex_session._existing_live_registration", existing)
    async def rebind(exact, name, registration_thread, database_url):
        assert (exact, name, registration_thread, database_url) == (
            binding, "/root", "registration-thread", "postgresql://exact",
        )
        return name
    monkeypatch.setattr("switchstand.codex_session._rebind_registration", rebind)

    assert prepare_runner(session) == (tmp_path / "runner.json", "postgresql://exact")
    assert calls[0][0] == "thread/start"
    assert calls[1] == ("mcpServer/tool/call", {
        "threadId": "registration-thread",
        "server": "switchstand",
        "tool": "agent_register",
        "arguments": {"api_version": "1", "name": "codex-head-exact"},
    })
    assert observed == [(binding, "/root", "postgresql://exact")]


def test_prepare_reconciles_committed_live_registration_before_retrying_helper(
    tmp_path: Path, monkeypatch,
) -> None:
    session = spec(tmp_path)
    binding = CodexBinding("thread-1", str(session.start_record), session.start_record.name)
    calls: list[str] = []

    class Client:
        def __init__(self, _codex: Path, _home: Path):
            pass

        def call(self, method: str, _arguments: dict[str, object]):
            calls.append(method)
            raise AssertionError("helper registration must not be retried")

        def close(self) -> None:
            calls.append("closed")

    async def existing(*_args):
        return "codex-head-exact"

    async def freeze(*_args):
        return tmp_path / "runner.json"

    monkeypatch.setattr("switchstand.codex_session.QueueClient", Client)
    monkeypatch.setattr("switchstand.codex_session.bind", lambda *_args: binding)
    monkeypatch.setattr("switchstand.codex_session._existing_live_registration", existing)
    monkeypatch.setattr("switchstand.codex_session._freeze_config", freeze)

    assert prepare_runner(session) == (tmp_path / "runner.json", "postgresql://exact")
    assert calls == ["closed"]


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
        lambda _spec: (tmp_path / "runner.json", "postgresql://unused"),
    )
    monkeypatch.setattr("switchstand.codex_session._runner_command", command)

    result = supervise(
        session, [sys.executable, "-c", "import time; time.sleep(1.4); raise SystemExit(7)"],
        dict(os.environ),
    )
    assert result == 7
    assert marker.read_text() == "xxs"


def test_supervisor_reports_continuous_registration_outage_once(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    session = spec(tmp_path)
    attempts = 0

    def unavailable(_spec: SessionSpec):
        nonlocal attempts
        attempts += 1
        raise ValueError("unavailable")

    monkeypatch.setattr("switchstand.codex_session.prepare_runner", unavailable)
    monkeypatch.setattr("switchstand.codex_session.MAX_RETRY_SECONDS", 0.01)

    result = supervise(
        session, [sys.executable, "-c", "import time; time.sleep(1.25)"],
        dict(os.environ),
    )

    assert result == 0
    assert attempts >= 2
    assert capsys.readouterr().err.count(
        "Wakeful registration unavailable; retrying"
    ) == 1


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
        "--environment-file", str(session.environment_file), "--", str(session.codex),
    ])
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 0
    assert observed[0].get("PYTHONPATH") == pythonpath
