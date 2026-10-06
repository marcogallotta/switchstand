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
from .codex_wakeful import CodexBinding, QueueClient, bind
from .secure_file import create_new_private_bytes, read_private_bytes

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
    mcp_server: str = "switchstand"


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


def prepare_runner(spec: SessionSpec) -> tuple[Path, str]:
    """Bind, register through authenticated MCP, and freeze one exact runner config."""
    client = QueueClient(spec.codex, spec.home)
    try:
        binding = bind(client, spec.home, spec.start_record)
        if not isinstance(binding, CodexBinding):
            raise TypeError(f"Codex thread binding is {binding}")
        response = client.call("mcpServer/tool/call", {
            "threadId": binding.thread_id,
            "server": spec.mcp_server,
            "tool": "agent_register",
            "arguments": {"api_version": "1", "name": spec.default_name},
        })
        registered = cast(dict[str, Any], response["structuredContent"])
        if registered.get("status") != "ok" or not isinstance(registered.get("name"), str):
            raise RuntimeError("authenticated mailbox registration did not converge")
        name = cast(str, registered["name"])
    finally:
        client.close()
    database_url = _database_url(spec.environment_file)
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


def _start_runner(config: Path, database_url: str) -> tuple[subprocess.Popen[bytes], int]:
    read_fd, write_fd = os.pipe()
    try:
        process = subprocess.Popen(
            _runner_command(config, read_fd),
            env=dict(os.environ) | {"DATABASE_URL": database_url},
            pass_fds=(read_fd,),
            start_new_session=True,
        )
    except Exception:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)
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
        codex = subprocess.Popen(command, env=environment, start_new_session=True)
        for signum in pending:
            deliver(signum)
        while True:
            returncode = codex.poll()
            if returncode is not None:
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
                _stop_runner(runner, lifeline)
                runner, lifeline = None, None
                if runner_started_at is not None and now - runner_started_at >= STABLE_RUNNER_SECONDS:
                    retry_delay = 1.0
                retry_at = now + retry_delay
                retry_delay = min(retry_delay * 2, MAX_RETRY_SECONDS)
                runner_started_at = None
            if runner is None and now >= retry_at:
                try:
                    if config is None or database_url is None:
                        config, database_url = prepare_runner(spec)
                    runner, lifeline = _start_runner(config, database_url)
                    runner_started_at = time.monotonic()
                except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                    print("Wakeful registration unavailable; retrying", file=sys.stderr)
                    retry_at = time.monotonic() + retry_delay
                    retry_delay = min(retry_delay * 2, MAX_RETRY_SECONDS)
            time.sleep(0.1)
        if termination is not None:
            return 128 + termination[0]
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
        for handled, old in previous.items():
            signal.signal(handled, old)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--home", type=Path, required=True)
    result.add_argument("--codex", type=Path, required=True)
    result.add_argument("--start-record", type=Path, required=True)
    result.add_argument("--default-name", required=True)
    result.add_argument("--environment-file", type=Path, required=True)
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
    )
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(spec.home)
    raise SystemExit(supervise(spec, command, environment))


if __name__ == "__main__":
    main()
