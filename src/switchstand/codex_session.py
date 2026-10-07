"""Default-off raw Codex registration and Wakeful session supervision."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from .agent_mailboxes import AgentMailboxState, chat_session_key
from .codex_app_server import (
    open_private_append,
    remote_command,
    start_app_server,
    stop_process,
)
from .codex_registration import prepare_registration
from .codex_wakeful import CodexBinding, QueueClient, bind
from .secure_file import atomic_replace_bytes, create_new_private_bytes, read_private_bytes

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


def _database_url(path: Path) -> str:
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError("Wakeful environment must be an owned mode-0600 regular file")
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "DATABASE_URL":
            result = value.strip().strip("'\"")
            if result:
                return result
    raise ValueError("Wakeful environment does not contain DATABASE_URL")


@contextmanager
def _configured_database(url: str):
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


async def _freeze_config(
    spec: SessionSpec, binding: CodexBinding, name: str, database_url: str,
) -> Path:
    from .chatgpt_edge import resource_service

    with _configured_database(database_url):
        async with resource_service() as (service, _runtime):
            if service.messages is None:
                raise ValueError("message state is unavailable")
            result = await AgentMailboxState(service.messages.engine).by_name(name)
    mailbox = result.mailbox
    if (
        result.status != "ok"
        or mailbox is None
        or mailbox.session_key != chat_session_key(f"codex:{binding.thread_id}")
    ):
        raise ValueError("registered mailbox does not match the exact Codex thread")
    body = json.dumps({
        "mailbox": mailbox.model_dump(mode="json"),
        "binding": asdict(binding),
        "codex_home": str(spec.home),
        "codex": str(spec.codex),
    }, sort_keys=True, separators=(",", ":")).encode()
    path = spec.home / f"wakeful-{spec.start_record.name.removeprefix('start-commit.')}.json"
    try:
        create_new_private_bytes(path, body)
    except FileExistsError:
        if read_private_bytes(path) != body:
            raise ValueError("Wakeful configuration identity changed") from None
    return path


async def _rebind_registration(
    binding: CodexBinding, name: str, registration_thread: str, database_url: str,
) -> str:
    """Move one authenticated helper registration onto the proven live Codex thread."""
    from .chatgpt_edge import resource_service

    with _configured_database(database_url):
        async with resource_service() as (service, _runtime):
            if service.messages is None:
                raise ValueError("message state is unavailable")
            mailboxes = AgentMailboxState(service.messages.engine)
            registered = await mailboxes.by_name(name)
            mailbox = registered.mailbox
            if (
                registered.status != "ok"
                or mailbox is None
                or mailbox.session_key != chat_session_key(f"codex:{registration_thread}")
            ):
                raise ValueError("helper registration does not match its exact Codex thread")
            moved = await mailboxes.takeover(
                name, mailbox.principal_key, f"codex:{binding.thread_id}"
            )
            if moved.status != "ok" or moved.mailbox is None:
                raise ValueError("live Codex mailbox rebind did not converge")
            return moved.mailbox.name


async def _existing_live_registration(
    binding: CodexBinding, name: str, registration_thread: str | None, database_url: str,
) -> str | None:
    from .chatgpt_edge import resource_service

    with _configured_database(database_url):
        async with resource_service() as (service, _runtime):
            if service.messages is None:
                raise ValueError("message state is unavailable")
            mailboxes = AgentMailboxState(service.messages.engine)
            result = await mailboxes.by_name(name)
            if result.status == "denied" and result.reason == "mailbox_not_found":
                return None
            mailbox = result.mailbox
            if result.status != "ok" or mailbox is None:
                raise ValueError("mailbox registration state is unavailable")
            if mailbox.session_key == chat_session_key(f"codex:{binding.thread_id}"):
                return mailbox.name
            if (
                registration_thread is not None
                and mailbox.session_key == chat_session_key(f"codex:{registration_thread}")
            ):
                moved = await mailboxes.takeover(
                    name, mailbox.principal_key, f"codex:{binding.thread_id}"
                )
                if moved.status == "ok" and moved.mailbox is not None:
                    return moved.mailbox.name
                raise ValueError("persisted helper registration did not converge")
            raise ValueError("mailbox name is bound to an unexpected session")


def _registration_thread_path(spec: SessionSpec) -> Path:
    suffix = spec.start_record.name.removeprefix("start-commit.")
    return spec.home / f"wakeful-registration-{suffix}"


def _pending_registration_thread(spec: SessionSpec) -> str | None:
    path = _registration_thread_path(spec)
    if not path.exists():
        return None
    value = read_private_bytes(path).decode().strip()
    if not value:
        raise ValueError("persisted helper registration thread is invalid")
    return value


def prepare_runner(spec: SessionSpec) -> tuple[Path, str]:
    """Legacy helper-thread registration retained until the cleanup layer lands."""
    database_url = _database_url(spec.environment_file)
    client = QueueClient(spec.codex, spec.home)
    try:
        binding = bind(client, spec.home, spec.start_record)
        if not isinstance(binding, CodexBinding):
            raise TypeError(f"Codex thread binding is {binding}")
        existing_name = asyncio.run(_existing_live_registration(
            binding, spec.default_name, _pending_registration_thread(spec), database_url,
        ))
        if existing_name is not None:
            return asyncio.run(_freeze_config(spec, binding, existing_name, database_url)), database_url
        started = client.call("thread/start", {"cwd": str(Path.cwd()), "ephemeral": True})
        registration_thread = cast(dict[str, Any], started["thread"])["id"]
        if not isinstance(registration_thread, str) or not registration_thread:
            raise ValueError("helper registration thread is unavailable")
        atomic_replace_bytes(
            _registration_thread_path(spec), (registration_thread + "\n").encode(),
        )
        response = client.call("mcpServer/tool/call", {
            "threadId": registration_thread, "server": spec.mcp_server,
            "tool": "agent_register",
            "arguments": {"api_version": "1", "name": spec.default_name},
        })
        registered = cast(dict[str, Any], response["structuredContent"])
        if registered.get("status") != "ok" or not isinstance(registered.get("name"), str):
            raise RuntimeError("authenticated mailbox registration did not converge")
        helper_name = cast(str, registered["name"])
    finally:
        client.close()
    name = asyncio.run(_rebind_registration(
        binding, helper_name, registration_thread, database_url,
    ))
    return asyncio.run(_freeze_config(spec, binding, name, database_url)), database_url


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
            app_server = start_app_server(
                spec.codex,
                spec.home,
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
        try:
            if stat.S_ISSOCK(spec.socket_path.lstat().st_mode):
                spec.socket_path.unlink()
        except FileNotFoundError:
            pass
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
