import fcntl
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

RECEIPT = "switchstand-run.json"


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


def process_start_token(pid: int, proc: Path = Path("/proc")) -> int:
    stat = (proc / str(pid) / "stat").read_text()
    tail = stat[stat.rindex(")") + 2:].split()
    return int(tail[19])


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
    repo: Path, branch: str, git_dir: Path
) -> Iterator[Callable[[UUID], RunReceipt]]:
    path = git_dir / RECEIPT
    lock = os.open(
        git_dir / "switchstand-run.lock",
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists() and inspect_receipt(path, repo, branch).status != "stopped":
            raise RuntimeError("previous managed run must be stopped before relaunch")
        yield lambda active_work_id: _write_receipt(repo, branch, active_work_id, git_dir)
    finally:
        os.close(lock)


def create_receipt(repo: Path, branch: str, active_work_id: UUID, git_dir: Path) -> RunReceipt:
    with reserve_run(repo, branch, git_dir) as record:
        return record(active_work_id)


def inspect_receipt(path: Path, repo: Path, branch: str, proc: Path = Path("/proc")) -> RunStatus:
    try:
        receipt = RunReceipt.model_validate(json.loads(path.read_text()))
    except (OSError, ValueError, ValidationError):
        return RunStatus(status="unknown")
    if receipt.worktree != str(repo) or receipt.branch != branch:
        return _known_status("unknown", receipt)
    try:
        current = process_start_token(receipt.pid, proc)
    except FileNotFoundError:
        return _known_status("stopped", receipt)
    except (OSError, ValueError):
        return _known_status("unknown", receipt)
    status = "running" if current == receipt.start_token else "lost"
    return _known_status(status, receipt)
