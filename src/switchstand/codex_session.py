"""Default-off raw Codex registration and Wakeful session supervision."""
from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
import tomllib
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast

from .agent_mailboxes import AgentMailboxState, agent_name_key, chat_session_key
from .codex_wakeful import CodexBinding, QueueClient, bind
from .secure_file import atomic_replace_bytes, create_new_private_bytes, read_private_bytes

CODEX_TERM_SECONDS = 10.0
RUNNER_EOF_SECONDS = 12.0
RUNNER_TERM_SECONDS = 2.0
MAX_RETRY_SECONDS = 10.0
STABLE_RUNNER_SECONDS = 30.0
THREAD_PLACEHOLDER = "__SWITCHSTAND_WAKEFUL_THREAD__"
SOCKET_PLACEHOLDER = "__SWITCHSTAND_WAKEFUL_SOCKET__"


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


def _open_private_append(path: Path) -> int:
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


def _diagnostic(spec: SessionSpec, event: str, **details: object) -> None:
    """Append one bounded private diagnostic event without disrupting the session."""
    record = json.dumps(
        {"time_ns": time.time_ns(), "event": event, **details},
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode() + b"\n"
    descriptor: int | None = None
    try:
        descriptor = _open_private_append(spec.log_path())
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
        "app_server_socket": str(spec.socket_path),
    }, sort_keys=True, separators=(",", ":")).encode()
    path = spec.home / f"wakeful-{spec.start_record.name.removeprefix('start-commit.')}.json"
    try:
        create_new_private_bytes(path, body)
    except FileExistsError:
        if read_private_bytes(path) != body:
            atomic_replace_bytes(path, body)
    return path


async def _existing_live_registration(
    binding: CodexBinding, name: str, database_url: str,
) -> tuple[Literal["missing", "exact", "takeover"], str | None]:
    """Reconcile a prior committed registration on the exact live thread."""
    from .chatgpt_edge import resource_service

    with _configured_database(database_url):
        async with resource_service() as (service, _runtime):
            if service.messages is None:
                raise ValueError("message state is unavailable")
            principal = await service.principal()
            if principal is None:
                raise ValueError("authenticated principal is unavailable")
            mailboxes = AgentMailboxState(service.messages.engine)
            result = await mailboxes.by_name(name)
            if result.status == "denied" and result.reason == "mailbox_not_found":
                return "missing", None
            mailbox = result.mailbox
            if result.status != "ok" or mailbox is None:
                raise ValueError("mailbox registration state is unavailable")
            if mailbox.session_key == chat_session_key(f"codex:{binding.thread_id}"):
                return "exact", mailbox.name
            if mailbox.principal_key == principal.key:
                return "takeover", mailbox.name
            raise ValueError("mailbox name is owned by another principal")


def _session_thread_path(spec: SessionSpec) -> Path:
    suffix = spec.start_record.name.removeprefix("start-commit.")
    return spec.home / f"wakeful-thread-{suffix}"


def _pending_session_thread(spec: SessionSpec) -> str | None:
    path = _session_thread_path(spec)
    if not path.exists():
        return None
    value = read_private_bytes(path).decode().strip()
    if not value:
        raise ValueError("persisted Codex thread is invalid")
    return value


def _start_thread(client: QueueClient, profile: dict[str, Any], developer: str) -> str:
    started = client.call("thread/start", {
        "cwd": str(Path.cwd()), "ephemeral": False,
        "developerInstructions": developer, "config": profile,
        "approvalPolicy": "never", "permissions": "switchstand-coordinator",
    })
    thread_id = cast(dict[str, Any], started["thread"])["id"]
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("launch-owned Codex thread is unavailable")
    return thread_id


def prepare_runner(spec: SessionSpec) -> tuple[Path, str, CodexBinding]:
    """Create, register, and freeze the exact thread before the TUI owns it."""
    database_url = _database_url(spec.environment_file)
    profile = tomllib.loads(spec.profile_path.read_text())
    developer = profile.get("developer_instructions")
    if not isinstance(developer, str) or str(spec.start_record) not in developer:
        raise ValueError("Codex profile does not bind the exact start record")
    client = QueueClient(spec.codex, spec.home, spec.socket_path)
    try:
        persisted = _pending_session_thread(spec)
        thread_id = persisted or _start_thread(client, profile, developer)
        binding = bind(client, spec.home, spec.start_record, thread_id)
        if not isinstance(binding, CodexBinding) and persisted is not None:
            # A launch can fail after creating an otherwise empty thread. Such a thread is
            # not necessarily recoverable after its owning app-server exits.
            _session_thread_path(spec).unlink()
            thread_id = _start_thread(client, profile, developer)
            binding = bind(client, spec.home, spec.start_record, thread_id)
        if not isinstance(binding, CodexBinding):
            raise TypeError(f"Codex thread binding is {binding}")
        registration_state, existing_name = asyncio.run(_existing_live_registration(
            binding, spec.default_name, database_url,
        ))
        if registration_state == "takeover":
            if agent_name_key(spec.default_name) == "root":
                raise ValueError("reserved /root mailbox requires explicit takeover")
            response = client.call("mcpServer/tool/call", {
                "threadId": binding.thread_id,
                "server": spec.mcp_server,
                "tool": "agent_takeover",
                "arguments": {"api_version": "1", "name": spec.default_name},
            })
            recovered = cast(dict[str, Any], response["structuredContent"])
            if recovered.get("status") != "ok" or not isinstance(
                recovered.get("name"), str,
            ):
                raise RuntimeError("authenticated mailbox recovery did not converge")
            existing_name = cast(str, recovered["name"])
        if registration_state in {"exact", "takeover"}:
            assert existing_name is not None
            name = existing_name
        else:
            response = client.call("mcpServer/tool/call", {
                "threadId": binding.thread_id,
                "server": spec.mcp_server,
                "tool": "agent_register",
                "arguments": {"api_version": "1", "name": spec.default_name},
            })
            registered = cast(dict[str, Any], response["structuredContent"])
            if registered.get("status") != "ok" or not isinstance(
                registered.get("name"), str,
            ):
                raise RuntimeError("authenticated mailbox registration did not converge")
            name = cast(str, registered["name"])
        config = asyncio.run(_freeze_config(spec, binding, name, database_url))
        atomic_replace_bytes(_session_thread_path(spec), (thread_id + "\n").encode())
    finally:
        client.close()
    return config, database_url, binding


def _prepare_socket(spec: SessionSpec) -> None:
    metadata = spec.home.lstat()
    if (
        spec.home.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("Codex home must be an owned mode-0700 directory")
    try:
        socket_metadata = spec.socket_path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(socket_metadata.st_mode) or socket_metadata.st_uid != os.getuid():
        raise ValueError("launch-owned app-server path is not a socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        result = probe.connect_ex(str(spec.socket_path))
        if result == 0:
            raise ValueError("launch-owned app-server is already active")
        if result not in {errno.ECONNREFUSED, errno.ENOENT}:
            raise OSError(result, "app-server socket liveness is unknown")
    finally:
        probe.close()
    spec.socket_path.unlink()


def _start_app_server(
    spec: SessionSpec, environment: dict[str, str], timeout: float = 10.0,
) -> subprocess.Popen[bytes]:
    """Start the one launch-owned app-server shared by registration, TUI, and Wakeful."""
    _prepare_socket(spec)
    log_descriptor = _open_private_append(spec.log_path("app-server"))
    try:
        process = subprocess.Popen(
            [str(spec.codex), "app-server", "--listen", f"unix://{spec.socket_path}"],
            env=environment, start_new_session=True, stdout=log_descriptor,
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
            try:
                socket_metadata = spec.socket_path.lstat()
                if stat.S_ISSOCK(socket_metadata.st_mode) and socket_metadata.st_uid == os.getuid():
                    spec.socket_path.chmod(0o600)
                    return process
            except FileNotFoundError:
                pass
            time.sleep(0.05)
        raise OSError("launch-owned app-server readiness timed out")
    except Exception:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=RUNNER_TERM_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        raise


def _remote_command(command: list[str], binding: CodexBinding, socket_path: Path) -> list[str]:
    if command.count(THREAD_PLACEHOLDER) != 1 or command.count(SOCKET_PLACEHOLDER) != 1:
        raise ValueError("Codex command does not contain exact Wakeful placeholders")
    return [
        binding.thread_id if value == THREAD_PLACEHOLDER
        else f"unix://{socket_path}" if value == SOCKET_PLACEHOLDER
        else value
        for value in command
    ]


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
            log_descriptor = _open_private_append(diagnostic_log)
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
            app_server = _start_app_server(spec, environment)
        except Exception as error:
            _diagnostic(
                spec, "launch_failed", stage="app_server_start",
                error_type=type(error).__name__, error=str(error),
            )
            raise
        _diagnostic(spec, "app_server_ready", socket=str(spec.socket_path))
        try:
            config, database_url, binding = prepare_runner(spec)
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
            _remote_command(command, binding, spec.socket_path),
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
        if app_server is not None and app_server.poll() is None:
            try:
                os.killpg(app_server.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                app_server.wait(timeout=CODEX_TERM_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(app_server.pid, signal.SIGKILL)
                app_server.wait()
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
