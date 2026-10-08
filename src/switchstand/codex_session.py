"""Default-off raw Codex registration and Wakeful session supervision."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .codex_app_server import (
    open_private_append,
    remote_command,
    remove_owned_socket_entry,
    start_app_server,
    stop_process,
)
from .codex_registration import prepare_registration

CODEX_TERM_SECONDS = 10.0
RUNNER_EOF_SECONDS = 12.0
RUNNER_TERM_SECONDS = 2.0
MAX_RETRY_SECONDS = 10.0
STABLE_RUNNER_SECONDS = 30.0
@dataclass(frozen=True)
class SessionSpec:
    home: Path
    codex: Path
    start_record: Path
    default_name: str
    environment_file: Path
    profile_path: Path
    mcp_server: str = "switchstand"

    @property
    def socket_path(self) -> Path:
        suffix = self.start_record.name.removeprefix("start-commit.")
        return self.home / f"wakeful-app-server-{suffix}.sock"

    def log_path(self, component: str = "session") -> Path:
        suffix = self.start_record.name.removeprefix("start-commit.")
        return self.home / f"wakeful-{component}-{suffix}.log"


def _diagnostic(spec: SessionSpec, event: str, **details: object) -> None:
    """Append one bounded private diagnostic event without disrupting the session."""
    record = json.dumps(
        {"time_ns": time.time_ns(), "event": event, **details},
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode() + b"\n"
    descriptor: int | None = None
    try:
        descriptor = open_private_append(spec.log_path())
        os.write(descriptor, record)
        os.fsync(descriptor)
    except (OSError, ValueError):
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _runner_command(config: Path, lifeline: int) -> tuple[str, ...]:
    return (
        sys.executable,
        "-c",
        (
            "import sys; sys.path.insert(0, sys.argv.pop(1)); "
            "from switchstand.codex_wakeful import inbound_main; inbound_main()"
        ),
        str(Path(__file__).resolve().parents[1]),
        "--config", str(config), "--lifeline-fd", str(lifeline),
    )


def _start_runner(
    config: Path, database_url: str, diagnostic_log: Path | None = None,
) -> tuple[subprocess.Popen[bytes], int]:
    read_fd, write_fd = os.pipe()
    log_descriptor: int | None = None
    try:
        if diagnostic_log is not None:
            log_descriptor = open_private_append(diagnostic_log)
        process = subprocess.Popen(
            _runner_command(config, read_fd),
            env=dict(os.environ) | {"DATABASE_URL": database_url},
            pass_fds=(read_fd,),
            start_new_session=True,
            stdout=log_descriptor,
            stderr=log_descriptor,
        )
    except Exception:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)
        if log_descriptor is not None:
            os.close(log_descriptor)
    return process, write_fd


def _stop_runner(process: subprocess.Popen[bytes] | None, lifeline: int | None) -> None:
    if lifeline is not None:
        try:
            os.close(lifeline)
        except OSError:
            pass
    if process is None or process.poll() is not None:
        if process is not None:
            process.wait()
        return
    try:
        process.wait(timeout=RUNNER_EOF_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=RUNNER_TERM_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def supervise(spec: SessionSpec, command: list[str], environment: dict[str, str]) -> int:
    """Own the exact Codex child and its restartable, session-bounded Wakeful runner."""
    forwarded = (
        signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM,
        signal.SIGTSTP, signal.SIGCONT, signal.SIGWINCH,
    )
    codex: subprocess.Popen[bytes] | None = None
    app_server: subprocess.Popen[bytes] | None = None
    runner: subprocess.Popen[bytes] | None = None
    lifeline: int | None = None
    previous: dict[signal.Signals, Any] = {}
    pending: list[int] = []
    termination: tuple[int, float] | None = None
    config: Path | None = None
    database_url: str | None = None
    retry_at = 0.0
    retry_delay = 1.0
    runner_started_at: float | None = None
    infrastructure_failure = False

    def deliver(signum: int) -> None:
        nonlocal termination
        assert codex is not None
        try:
            os.killpg(codex.pid, signum)
        except ProcessLookupError:
            pass
        if signum in {signal.SIGHUP, signal.SIGQUIT, signal.SIGTERM} and termination is None:
            termination = (signum, time.monotonic() + CODEX_TERM_SECONDS)
        if signum == signal.SIGTSTP:
            os.kill(os.getpid(), signal.SIGSTOP)

    def forward(signum: int, _frame: object) -> None:
        if codex is None:
            pending.append(signum)
        else:
            deliver(signum)

    try:
        for handled in forwarded:
            previous[handled] = signal.signal(handled, forward)
        try:
            profile = tomllib.loads(spec.profile_path.read_text())
            hooks = profile.get("hooks")
            if not isinstance(hooks, dict):
                raise TypeError("generated Codex profile has no hooks table")
            app_server = start_app_server(
                spec.codex,
                spec.home,
                cast(dict[str, object], hooks),
                spec.socket_path,
                spec.log_path("app-server"),
                environment,
                term_seconds=RUNNER_TERM_SECONDS,
            )
        except Exception as error:
            _diagnostic(
                spec, "launch_failed", stage="app_server_start",
                error_type=type(error).__name__, error=str(error),
            )
            raise
        _diagnostic(spec, "app_server_ready", socket=str(spec.socket_path))
        try:
            config, database_url, binding = prepare_registration(spec)
            _diagnostic(
                spec, "registration_ready", mailbox=spec.default_name,
                thread_id=binding.thread_id,
            )
        except Exception as error:
            _diagnostic(
                spec, "launch_failed", stage="registration",
                error_type=type(error).__name__, error=str(error),
            )
            raise
        codex = subprocess.Popen(
            remote_command(command, binding.thread_id, spec.socket_path),
            env=environment, start_new_session=True,
        )
        for signum in pending:
            deliver(signum)
        while True:
            returncode = codex.poll()
            if returncode is not None:
                break
            if app_server.poll() is not None:
                infrastructure_failure = True
                _diagnostic(
                    spec, "app_server_exited", status=app_server.returncode,
                )
                break
            now = time.monotonic()
            if termination is not None and now >= termination[1]:
                try:
                    os.killpg(codex.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                returncode = codex.wait()
                break
            if runner is not None and runner.poll() is not None:
                _diagnostic(
                    spec, "notification_runner_exited", status=runner.returncode,
                    next_retry_seconds=retry_delay,
                )
                _stop_runner(runner, lifeline)
                runner, lifeline = None, None
                if runner_started_at is not None and now - runner_started_at >= STABLE_RUNNER_SECONDS:
                    retry_delay = 1.0
                retry_at = now + retry_delay
                retry_delay = min(retry_delay * 2, MAX_RETRY_SECONDS)
                runner_started_at = None
            if runner is None and now >= retry_at:
                try:
                    assert config is not None and database_url is not None
                    runner, lifeline = _start_runner(
                        config, database_url, spec.log_path("runner"),
                    )
                    runner_started_at = time.monotonic()
                    _diagnostic(spec, "notification_runner_started", pid=runner.pid)
                except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                    _diagnostic(
                        spec, "notification_runner_start_failed",
                        error_type=type(error).__name__, error=str(error),
                        next_retry_seconds=retry_delay,
                    )
                    retry_at = time.monotonic() + retry_delay
                    retry_delay = min(retry_delay * 2, MAX_RETRY_SECONDS)
            time.sleep(0.1)
        if infrastructure_failure:
            return 1
        if termination is not None:
            return 128 + termination[0]
        assert returncode is not None
        return returncode if returncode >= 0 else 128 - returncode
    finally:
        _stop_runner(runner, lifeline)
        if codex is not None and codex.poll() is None:
            try:
                os.killpg(codex.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                codex.wait(timeout=CODEX_TERM_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(codex.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                codex.wait()
        if app_server is not None:
            stop_process(app_server, CODEX_TERM_SECONDS)
        remove_owned_socket_entry(spec.socket_path)
        for handled, old in previous.items():
            signal.signal(handled, old)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--home", type=Path, required=True)
    result.add_argument("--codex", type=Path, required=True)
    result.add_argument("--start-record", type=Path, required=True)
    result.add_argument("--default-name", required=True)
    result.add_argument("--environment-file", type=Path, required=True)
    result.add_argument("--profile-path", type=Path, required=True)
    result.add_argument("codex_args", nargs=argparse.REMAINDER)
    return result


def main() -> None:
    arguments = parser().parse_args()
    command = list(arguments.codex_args)
    if command[:1] == ["--"]:
        command.pop(0)
    if not command:
        parser().error("Codex command is required after --")
    spec = SessionSpec(
        home=arguments.home.resolve(strict=True),
        codex=arguments.codex.resolve(strict=True),
        start_record=arguments.start_record.resolve(strict=True),
        default_name=arguments.default_name,
        environment_file=arguments.environment_file.resolve(strict=True),
        profile_path=arguments.profile_path.resolve(strict=True),
    )
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(spec.home)
    raise SystemExit(supervise(spec, command, environment))


if __name__ == "__main__":
    main()
