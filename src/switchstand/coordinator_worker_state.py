"""Durable identity, state, and candidate proof for Coordinator workers."""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import model_validator

from .contracts import ClosedModel
from .run import RECEIPT

COMMAND_TIMEOUT_SECONDS = 15


class WorkerSpawnResult(ClosedModel):
    status: Literal["started", "replayed", "denied", "unknown"]
    spawn_id: UUID | None = None
    work_id: UUID
    writer: str | None = None
    base_sha: str | None = None
    reason: str


class WorkerStatusResult(ClosedModel):
    status: Literal["running", "completed", "failed", "cancelled", "unknown"]
    spawn_id: UUID
    work_id: UUID | None = None
    writer: str | None = None
    branch: str | None = None
    base_sha: str | None = None
    candidate_sha: str | None = None
    summary: str | None = None
    reason: str


class WorkerCancelResult(ClosedModel):
    status: Literal["cancelled", "already_terminal", "unknown"]
    spawn_id: UUID
    reason: str


class UnitState(ClosedModel):
    load: str
    active: str
    sub: str


class WorkerRecord(ClosedModel):
    version: Literal[1, 2] = 2
    spawn_id: UUID
    work_id: UUID
    objective: str
    writer: str
    branch: str
    base_sha: str
    git_common: str | None = None
    unit: str
    log: str
    started_at: float
    command: tuple[str, ...]
    phase: Literal["PREPARED", "RUNNING", "TERMINAL"] = "PREPARED"
    exit_status: int | None = None
    cancelled: bool = False

    @model_validator(mode="after")
    def validate_git_common_version(self) -> Self:
        if self.version == 1 and self.git_common is not None:
            raise ValueError("legacy worker record cannot bind git_common")
        if self.version == 2 and self.git_common is None:
            raise ValueError("worker record v2 requires git_common")
        if self.git_common is not None:
            common = Path(self.git_common)
            if not common.is_absolute() or ".." in common.parts:
                raise ValueError("worker record git_common must be an absolute canonical path")
        return self


class WorkerStore:
    """Private durable records and Git identity checks, with fail-closed reads."""

    def __init__(self, home: Path, state_root: Path | None = None) -> None:
        self.home = home.resolve(strict=True)
        self.repo = self.home / "switchstand"
        self.root = state_root or self.home / ".local/state/switchstand/coordinator-workers"

    @contextmanager
    def locked(self) -> Generator[None]:
        self.secure()
        descriptor = os.open(
            self.root / "spawn.lock",
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def secure(self) -> None:
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = self.root.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise PermissionError("unsafe_coordinator_worker_state")

    def load(self, spawn_id: UUID) -> WorkerRecord | None:
        path = self.root / f"{spawn_id}.json"
        return self._read(path) if path.exists() else None

    def records(self) -> Generator[WorkerRecord]:
        for path in self.root.glob("*.json"):
            record = self._read(path)
            if record is None:
                raise ValueError("coordinator_worker_state_corrupt")
            yield record

    @staticmethod
    def _read(path: Path) -> WorkerRecord | None:
        try:
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > 32_768
            ):
                return None
            return WorkerRecord.model_validate_json(path.read_text())
        except OSError, ValueError:
            return None

    def write(self, record: WorkerRecord, *, replace: bool = False) -> None:
        path = self.root / f"{record.spawn_id}.json"
        temporary = self.root / f".{record.spawn_id}.{os.getpid()}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(record.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path) if replace else os.link(temporary, path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def candidate_identity(self, work_id: UUID) -> tuple[str, Path, str, str]:
        writer = self.home / ".local/state/switchstand/worktrees" / f"switchstand-task-{work_id}"
        branch = f"v2-task-{work_id}"
        base, expected_common = self._active_control_identity()
        if writer.exists():
            head, identity = self._writer_identity(writer, branch, Path(expected_common))
            if not identity:
                raise ValueError("worker_writer_identity_mismatch")
            return head, writer, branch, expected_common
        return base, writer, branch, expected_common

    def _active_control_identity(self) -> tuple[str, str]:
        manifest = self.home / ".local/state/switchstand/control/manifest"
        metadata = manifest.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("isolated_control_unavailable")
        lines = manifest.read_text().splitlines()
        if any("=" not in line for line in lines):
            raise ValueError("isolated_control_unavailable")
        fields = dict(line.split("=", 1) for line in lines)
        base = fields.get("control_sha", "")
        control_path = fields.get("control_path", "")
        if fields.get("state") != "ACTIVE" or len(base) != 40 or not control_path:
            raise ValueError("isolated_control_unavailable")
        int(base, 16)
        control = Path(control_path)
        common = self.git(
            control, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).stdout.strip()
        head = self.git(control, "rev-parse", "--verify", "HEAD").stdout.strip()
        if head != base or not common:
            raise ValueError("isolated_control_unavailable")
        return base, str(Path(common).resolve())

    def candidate(self, record: WorkerRecord) -> tuple[str | None, bool, bool]:
        writer = Path(record.writer)
        if not writer.is_dir() or writer.is_symlink():
            return None, False, False
        if record.version == 2:
            if record.git_common is None:  # enforced by WorkerRecord validation
                return None, False, False
            expected_common = Path(record.git_common)
        else:
            expected_common = self.repo / ".git"
        if record.version == 2:
            try:
                if expected_common.resolve(strict=True) != expected_common:
                    return None, False, False
            except OSError:
                return None, False, False
        head, identity = self._writer_identity(writer, record.branch, expected_common)
        if not identity:
            return None, False, False
        clean = not self.git(
            writer, "status", "--porcelain", "--untracked-files=all"
        ).stdout.strip()
        descendant = (
            self.git(
                writer,
                "merge-base",
                "--is-ancestor",
                record.base_sha,
                head,
                check=False,
            ).returncode
            == 0
        )
        return head, clean, descendant

    def _writer_identity(
        self, writer: Path, branch: str, expected_common: Path
    ) -> tuple[str, bool]:
        root, observed_branch, common = self.git(
            writer,
            "rev-parse",
            "--show-toplevel",
            "--abbrev-ref",
            "HEAD",
            "--path-format=absolute",
            "--git-common-dir",
        ).stdout.splitlines()
        head = self.git(writer, "rev-parse", "--verify", "HEAD").stdout.strip()
        identity = (
            Path(root).resolve() == writer.resolve()
            and observed_branch == branch
            and Path(common).resolve() == expected_common.resolve()
        )
        return head, identity

    def run_receipt(self, record: WorkerRecord) -> Path | None:
        writer = Path(record.writer)
        if not writer.is_dir():
            return None
        git_dir = Path(self.git(writer, "rev-parse", "--absolute-git-dir").stdout.strip()).resolve()
        receipt = git_dir / RECEIPT
        return receipt if receipt.exists() else None

    @staticmethod
    def git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(repo),
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                *arguments,
            ],
            check=check,
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/nonexistent",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
