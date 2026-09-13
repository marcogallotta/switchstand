from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


class CandidateError(RuntimeError):
    """The persistent candidate cannot be attached safely."""


class WorkspaceMissing(CandidateError):
    """A recorded candidate branch exists but its persistent worktree is missing."""


class WorkspaceUnknown(CandidateError):
    """Candidate state exists but cannot be proven to belong to this repository/work."""


class WriterBusy(CandidateError):
    """Another managed writer currently owns this candidate."""


def _git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=check,
        text=True,
        capture_output=True,
    )


def _state_home(explicit: Path | None = None) -> Path:
    if explicit is not None:
        root = explicit
    elif value := os.environ.get("XDG_STATE_HOME"):
        root = Path(value)
    elif value := os.environ.get("HOME"):
        root = Path(value) / ".local" / "state"
    else:
        raise CandidateError("candidate workspace requires HOME or XDG_STATE_HOME")
    if not root.is_absolute():
        raise CandidateError(f"candidate state directory must be absolute: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root.resolve(strict=True)


def _repository_identity(repo: Path) -> tuple[Path, str]:
    root = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve(strict=True)
    common = Path(
        _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    ).resolve(strict=True)
    origin = _git(root, "remote", "get-url", "origin").stdout.strip()
    if not origin:
        raise CandidateError("candidate workspace requires an exact origin repository identity")
    fingerprint = hashlib.sha256(origin.encode()).hexdigest()
    return common, fingerprint


def _branch_exists(repo: Path, branch: str) -> bool:
    return _git(
        repo,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
        check=False,
    ).returncode == 0


def _worktree_record(repo: Path, target: Path) -> tuple[bool, bool]:
    registered = False
    locked = False
    current = False
    for line in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree ")).resolve() == target
            registered = registered or current
            continue
        if not line:
            current = False
            continue
        if current and line.startswith("locked"):
            locked = True
    return registered, locked


def _read_identity(git_dir: Path, name: str) -> str:
    try:
        return (git_dir / name).read_text().strip()
    except OSError as error:
        raise WorkspaceUnknown(f"candidate identity metadata is missing: {name}") from error


def _write_identity(git_dir: Path, name: str, value: str) -> None:
    path = git_dir / name
    temporary = git_dir / f".{name}.{os.getpid()}.tmp"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "w") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class CandidateWorkspace:
    path: Path
    branch: str
    git_dir: Path
    common_dir: Path
    work_id: UUID
    green_sha: str
    repository_fingerprint: str

    @contextmanager
    def writer(self) -> Generator[None, None, None]:
        descriptor = os.open(
            self.git_dir / "switchstand-writer.lock",
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise WriterBusy(f"candidate already has an active writer: {self.work_id}") from error
            yield
        finally:
            os.close(descriptor)


def _attach(
    repo: Path,
    target: Path,
    branch: str,
    common_dir: Path,
    work_id: UUID,
    repository_fingerprint: str,
) -> CandidateWorkspace:
    if target.is_symlink() or not target.is_dir():
        raise WorkspaceUnknown(f"candidate path is not a persistent directory: {target}")
    try:
        actual_root = Path(_git(target, "rev-parse", "--show-toplevel").stdout.strip()).resolve(
            strict=True
        )
        actual_branch = _git(target, "branch", "--show-current").stdout.strip()
        actual_head = _git(target, "rev-parse", "HEAD").stdout.strip()
        actual_git_dir = Path(
            _git(target, "rev-parse", "--absolute-git-dir").stdout.strip()
        ).resolve(strict=True)
        actual_common = Path(
            _git(target, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        ).resolve(strict=True)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise WorkspaceUnknown("candidate Git identity cannot be read safely") from error
    if actual_root != target or actual_branch != branch or actual_common != common_dir:
        raise WorkspaceUnknown("candidate path/branch/repository identity does not match the recorded work")
    if actual_git_dir == actual_common:
        raise WorkspaceUnknown("candidate must be a linked worktree")

    recorded_work_id = _read_identity(actual_git_dir, "switchstand-work-id")
    recorded_repository = _read_identity(actual_git_dir, "switchstand-repository-fingerprint")
    green_sha = _read_identity(actual_git_dir, "switchstand-green-sha")
    if recorded_work_id != str(work_id) or recorded_repository != repository_fingerprint:
        raise WorkspaceUnknown("candidate metadata does not match the requested repository and WorkId")
    if _git(repo, "cat-file", "-e", f"{green_sha}^{{commit}}", check=False).returncode != 0:
        raise WorkspaceUnknown("candidate baseline commit is unavailable")
    if actual_head != green_sha and _git(
        target, "merge-base", "--is-ancestor", green_sha, actual_head, check=False
    ).returncode != 0:
        raise WorkspaceUnknown("candidate HEAD no longer descends from its recorded baseline")

    registered, locked = _worktree_record(repo, target)
    if not registered:
        raise WorkspaceUnknown("candidate is not registered by the requesting repository")
    if not locked:
        _git(repo, "worktree", "lock", "--reason", f"switchstand candidate {work_id}", str(target))
    return CandidateWorkspace(
        path=target,
        branch=branch,
        git_dir=actual_git_dir,
        common_dir=actual_common,
        work_id=work_id,
        green_sha=green_sha,
        repository_fingerprint=repository_fingerprint,
    )


def prepare_candidate(
    repo: Path,
    work_id: UUID,
    starting_sha: str,
    *,
    state_home: Path | None = None,
) -> CandidateWorkspace:
    repo = repo.resolve(strict=True)
    common_dir, repository_fingerprint = _repository_identity(repo)
    if _git(repo, "cat-file", "-e", f"{starting_sha}^{{commit}}", check=False).returncode != 0:
        raise CandidateError(f"candidate starting commit is unavailable: {starting_sha}")

    root = _state_home(state_home) / "switchstand" / "worktrees" / repository_fingerprint
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = root.resolve(strict=True)
    target = root / str(work_id)
    branch = f"switchstand/work-{work_id}"
    target_exists = target.exists() or target.is_symlink()
    branch_exists = _branch_exists(repo, branch)

    if target_exists != branch_exists:
        if branch_exists:
            raise WorkspaceMissing(
                f"candidate branch exists but persistent contents are missing: {target}"
            )
        raise WorkspaceUnknown(f"persistent candidate path exists without its branch: {target}")

    if not target_exists:
        _git(repo, "worktree", "add", "-b", branch, str(target), starting_sha)
        git_dir = Path(
            _git(target, "rev-parse", "--absolute-git-dir").stdout.strip()
        ).resolve(strict=True)
        _write_identity(git_dir, "switchstand-green-sha", starting_sha)
        _write_identity(git_dir, "switchstand-work-id", str(work_id))
        _write_identity(git_dir, "switchstand-repository-fingerprint", repository_fingerprint)
        _git(repo, "worktree", "lock", "--reason", f"switchstand candidate {work_id}", str(target))

    return _attach(repo, target, branch, common_dir, work_id, repository_fingerprint)
