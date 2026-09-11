import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

RECEIPT = "switchstand-run.json"
GRACE_SECONDS = 2.0
FORCE_SECONDS = 2.0


class RunReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    run_id: UUID
    active_work_id: UUID
    worktree: str
    branch: str
    pid: int = Field(gt=0)
    start_token: int = Field(ge=0)
    started_at: datetime


class RunStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["running", "stopped", "lost", "unknown"]
    run_id: UUID | None = None
    active_work_id: UUID | None = None
    branch: str | None = None


def _known_status(
    status: Literal["running", "stopped", "lost", "unknown"], receipt: RunReceipt
) -> RunStatus:
    return RunStatus(
        status=status,
        run_id=receipt.run_id,
        active_work_id=receipt.active_work_id,
        branch=receipt.branch,
    )


def _process_identity(pid: int, proc: Path = Path("/proc")) -> tuple[str, int]:
    stat = (proc / str(pid) / "stat").read_text()
    tail = stat[stat.rindex(")") + 2:].split()
    return tail[0], int(tail[19])


def process_start_token(pid: int, proc: Path = Path("/proc")) -> int:
    return _process_identity(pid, proc)[1]


def _read_receipt(path: Path) -> RunReceipt | None:
    try:
        return RunReceipt.model_validate(json.loads(path.read_text()))
    except (IndexError, OSError, ValueError, ValidationError):
        return None


def _write_receipt(
    repo: Path, branch: str, active_work_id: UUID, git_dir: Path
) -> RunReceipt:
    receipt = RunReceipt(
        run_id=uuid4(),
        active_work_id=active_work_id,
        worktree=str(repo),
        branch=branch,
        pid=os.getpid(),
        start_token=process_start_token(os.getpid()),
        started_at=datetime.now(UTC),
    )
    path = git_dir / RECEIPT
    temporary = git_dir / f".{RECEIPT}.{receipt.run_id}.tmp"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "w") as stream:
            stream.write(receipt.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(git_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return receipt


@contextmanager
def reserve_run(
    repo: Path,
    branch: str,
    git_dir: Path,
    reclaim: Callable[[RunReceipt], None] | None = None,
) -> Generator[Callable[[UUID], RunReceipt]]:
    path = git_dir / RECEIPT
    lock = os.open(
        git_dir / "switchstand-run.lock",
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            receipt = _read_receipt(path)
            status = inspect_receipt(path, repo, branch).status
            if receipt is None or status not in {"stopped", "lost"}:
                raise RuntimeError("previous managed run must be stopped before relaunch")
            if reclaim is not None:
                reclaim(receipt)
        yield lambda active_work_id: _write_receipt(repo, branch, active_work_id, git_dir)
    finally:
        os.close(lock)


def create_receipt(repo: Path, branch: str, active_work_id: UUID, git_dir: Path) -> RunReceipt:
    with reserve_run(repo, branch, git_dir) as record:
        return record(active_work_id)


def inspect_receipt(path: Path, repo: Path, branch: str, proc: Path = Path("/proc")) -> RunStatus:
    receipt = _read_receipt(path)
    if receipt is None:
        return RunStatus(status="unknown")
    if receipt.worktree != str(repo) or receipt.branch != branch:
        return _known_status("unknown", receipt)
    try:
        state, current = _process_identity(receipt.pid, proc)
    except FileNotFoundError:
        return _known_status("stopped", receipt)
    except (IndexError, OSError, ValueError):
        return _known_status("unknown", receipt)
    status = "running" if current == receipt.start_token else "lost"
    if state in {"X", "Z"} and status == "running":
        status = "stopped"
    return _known_status(status, receipt)


def _wait_for_exit(pidfd: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            if poller.poll(max(1, int(remaining * 1000))):
                return True
        except InterruptedError:
            pass
    return False


def stop_receipt(path: Path, repo: Path, branch: str, proc: Path = Path("/proc")) -> RunStatus:
    """Stop only the exact process identity recorded for this worktree."""
    try:
        lock = os.open(
            path.with_name("switchstand-run.lock"),
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError:
        return RunStatus(status="unknown")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError:
            return RunStatus(status="unknown")
        receipt = _read_receipt(path)
        if receipt is None:
            return RunStatus(status="unknown")
        if receipt.worktree != str(repo) or receipt.branch != branch:
            return _known_status("unknown", receipt)
        if receipt.pid in {1, os.getpid()}:
            return _known_status("unknown", receipt)
        try:
            pidfd = os.pidfd_open(receipt.pid)
        except ProcessLookupError:
            return _known_status("stopped", receipt)
        except OSError:
            return _known_status("unknown", receipt)
        try:
            try:
                _, token = _process_identity(receipt.pid, proc)
            except FileNotFoundError:
                return _known_status("stopped", receipt)
            except (IndexError, OSError, ValueError):
                return _known_status("unknown", receipt)
            if token != receipt.start_token:
                return _known_status("lost", receipt)
            for sent, timeout in ((signal.SIGTERM, GRACE_SECONDS),
                                  (signal.SIGKILL, FORCE_SECONDS)):
                try:
                    signal.pidfd_send_signal(pidfd, sent)
                except ProcessLookupError:
                    return _known_status("stopped", receipt)
                except OSError:
                    return _known_status("unknown", receipt)
                try:
                    exited = _wait_for_exit(pidfd, timeout)
                except OSError:
                    return _known_status("unknown", receipt)
                if exited:
                    return _known_status("stopped", receipt)
            return _known_status("unknown", receipt)
        finally:
            try:
                os.close(pidfd)
            except OSError:
                pass
    finally:
        try:
            os.close(lock)
        except OSError:
            pass


def main() -> None:
    if len(sys.argv) != 1:
        raise SystemExit("usage: switchstand-run-stop")
    repo = Path.cwd().resolve()
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir",
             "--git-common-dir", "--abbrev-ref", "HEAD"],
            cwd=repo, check=True, text=True, capture_output=True,
        )
        root, git_value, common_value, branch = completed.stdout.splitlines()
        git_dir, common_dir = (Path(value).resolve(strict=True)
                               for value in (git_value, common_value))
        if Path(root).resolve() != repo or git_dir == common_dir or branch == "HEAD":
            raise ValueError("managed stop requires a linked writer worktree branch")
        result = stop_receipt(git_dir / RECEIPT, repo, branch)
    except (OSError, ValueError, subprocess.SubprocessError):
        result = RunStatus(status="unknown")
    print(result.model_dump_json())
    raise SystemExit(0 if result.status in {"stopped", "lost"} else 1)


if __name__ == "__main__":
    main()
