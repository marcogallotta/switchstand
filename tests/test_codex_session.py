from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from switchstand.codex_app_server import SOCKET_PLACEHOLDER, THREAD_PLACEHOLDER
from switchstand.codex_session import (
    SessionSpec,
    _start_runner,
    _stop_runner,
    main,
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
    profile.write_text(f'developer_instructions = "use {start}"\n[hooks]\n')
    return SessionSpec(
        home=tmp_path,
        codex=codex,
        start_record=start,
        default_name="codex-head-exact",
        environment_file=private_environment(tmp_path / ".env"),
        profile_path=profile,
    )


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


def test_runner_diagnostics_do_not_leak_to_parent_terminal(
    tmp_path: Path, monkeypatch, capfd,
) -> None:
    diagnostic_log = tmp_path / "runner.log"

    def command(_config: Path, _descriptor: int) -> tuple[str, ...]:
        program = (
            "import sys; "
            "print('runner-stdout'); "
            "print('runner-stderr', file=sys.stderr)"
        )
        return sys.executable, "-c", program

    monkeypatch.setattr("switchstand.codex_session._runner_command", command)
    runner, lifeline = _start_runner(
        tmp_path / "config", "postgresql://unused", diagnostic_log,
    )
    runner.wait(timeout=2)
    _stop_runner(runner, lifeline)

    captured = capfd.readouterr()
    assert "runner-stdout" not in captured.out
    assert "runner-stderr" not in captured.err
    assert diagnostic_log.stat().st_mode & 0o777 == 0o600
    assert set(diagnostic_log.read_text().splitlines()) == {
        "runner-stdout", "runner-stderr",
    }


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
        "switchstand.codex_session.prepare_registration",
        lambda _spec: (
            tmp_path / "runner.json", "postgresql://unused",
            CodexBinding("thread-1", str(session.start_record), session.start_record.name),
        ),
    )
    monkeypatch.setattr("switchstand.codex_session._runner_command", command)
    monkeypatch.setattr(
        "switchstand.codex_session.start_app_server",
        lambda *_args, **_kwargs: subprocess.Popen(
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

    monkeypatch.setattr("switchstand.codex_session.prepare_registration", unavailable)
    monkeypatch.setattr(
        "switchstand.codex_session.start_app_server",
        lambda *_args, **_kwargs: subprocess.Popen(
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
        "switchstand.codex_session.start_app_server", lambda *_args, **_kwargs: app_server,
    )
    monkeypatch.setattr(
        "switchstand.codex_session.prepare_registration",
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
