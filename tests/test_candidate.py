import shutil
import subprocess
from dataclasses import fields
from pathlib import Path
from uuid import UUID

import pytest

from switchstand.candidate import (
    CandidateIdentity,
    WorkspaceAbsent,
    WorkspaceMissing,
    WorkspaceUnknown,
    WriterBusy,
    inspect_candidate,
    prepare_candidate,
)

WORK_ID = UUID("00000000-0000-0000-0000-000000000001")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()


def repository(path: Path, origin: str = "https://example.test/switchstand.git") -> tuple[Path, str]:
    if shutil.which("git") is None:
        pytest.skip("git is required for real persistent-candidate tests")
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.test")
    git(path, "config", "user.name", "Switchstand Test")
    (path / "file.txt").write_text("one\n")
    git(path, "add", "file.txt")
    git(path, "commit", "-qm", "initial")
    git(path, "remote", "add", "origin", origin)
    return path, git(path, "rev-parse", "HEAD")


def test_candidate_is_persistent_and_reuses_dirty_progress(tmp_path: Path) -> None:
    repo, base = repository(tmp_path / "repo")
    state = tmp_path / "state"
    first = prepare_candidate(repo, WORK_ID, base, state_home=state)
    assert first.path.is_relative_to(state)
    assert "/tmp/switchstand-" not in str(first.path)
    assert first.branch == f"switchstand/work-{WORK_ID}"

    (first.path / "untracked.txt").write_text("progress\n")
    (first.path / "file.txt").write_text("dirty progress\n")
    second = prepare_candidate(repo, WORK_ID, base, state_home=state)
    assert second == first
    assert (second.path / "untracked.txt").read_text() == "progress\n"
    assert (second.path / "file.txt").read_text() == "dirty progress\n"

    record = git(repo, "worktree", "list", "--porcelain")
    assert f"worktree {first.path}" in record
    assert "locked" in record.split(f"worktree {first.path}", 1)[1].split("\n\n", 1)[0]


def test_candidate_does_not_reset_when_requested_base_moves(tmp_path: Path) -> None:
    repo, first_base = repository(tmp_path / "repo")
    state = tmp_path / "state"
    candidate = prepare_candidate(repo, WORK_ID, first_base, state_home=state)
    (repo / "file.txt").write_text("two\n")
    git(repo, "commit", "-am", "next", "-q")
    moved_base = git(repo, "rev-parse", "HEAD")

    resumed = prepare_candidate(repo, WORK_ID, moved_base, state_home=state)
    assert resumed.green_sha == first_base
    assert git(resumed.path, "rev-parse", "HEAD") == first_base
    assert resumed.path == candidate.path


def test_missing_persistent_contents_are_explicit(tmp_path: Path) -> None:
    repo, base = repository(tmp_path / "repo")
    state = tmp_path / "state"
    candidate = prepare_candidate(repo, WORK_ID, base, state_home=state)
    git(repo, "worktree", "unlock", str(candidate.path))
    git(repo, "worktree", "remove", "--force", str(candidate.path))
    with pytest.raises(WorkspaceMissing, match="persistent contents are missing"):
        prepare_candidate(repo, WORK_ID, base, state_home=state)


def test_corrupt_existing_worktree_identity_is_unknown(tmp_path: Path) -> None:
    repo, base = repository(tmp_path / "repo")
    state = tmp_path / "state"
    candidate = prepare_candidate(repo, WORK_ID, base, state_home=state)
    git(repo, "worktree", "unlock", str(candidate.path))
    (candidate.path / ".git").write_text("gitdir: /definitely/missing\n")
    with pytest.raises(WorkspaceUnknown, match="Git identity cannot be read safely"):
        prepare_candidate(repo, WORK_ID, base, state_home=state)


def test_foreign_clone_cannot_adopt_same_repository_work_id(tmp_path: Path) -> None:
    origin = "https://example.test/switchstand.git"
    repo, base = repository(tmp_path / "repo-a", origin)
    state = tmp_path / "state"
    prepare_candidate(repo, WORK_ID, base, state_home=state)

    foreign = tmp_path / "repo-b"
    git(tmp_path, "clone", "-q", str(repo), str(foreign))
    git(foreign, "remote", "set-url", "origin", origin)
    with pytest.raises(WorkspaceUnknown, match="persistent candidate path|repository identity"):
        prepare_candidate(foreign, WORK_ID, base, state_home=state)


def test_candidate_writer_lock_is_non_blocking(tmp_path: Path) -> None:
    repo, base = repository(tmp_path / "repo")
    candidate = prepare_candidate(repo, WORK_ID, base, state_home=tmp_path / "state")
    with candidate.writer(), pytest.raises(WriterBusy, match="active writer"), candidate.writer():
        raise AssertionError("second writer unexpectedly acquired the candidate")


def test_inspect_candidate_observes_current_git_identity_without_repair(tmp_path: Path) -> None:
    repo, base = repository(tmp_path / "repo")
    state = tmp_path / "state"
    candidate = prepare_candidate(repo, WORK_ID, base, state_home=state)
    git(repo, "worktree", "unlock", str(candidate.path))
    (candidate.path / "file.txt").write_text("next\n")
    git(candidate.path, "commit", "-am", "next", "-q")
    current_head = git(candidate.path, "rev-parse", "HEAD")
    registration = git(repo, "worktree", "list", "--porcelain")
    metadata = (candidate.git_dir / "switchstand-work-id").read_bytes()

    observed = inspect_candidate(repo, WORK_ID, state_home=state)
    assert observed.green_sha == base
    assert observed.head_sha == current_head
    assert observed.head_sha != base
    assert type(observed) is CandidateIdentity
    assert {field.name for field in fields(observed)} == {
        "branch", "work_id", "green_sha", "head_sha",
    }
    assert not any(
        hasattr(observed, name) for name in ("writer", "git_dir", "path", "workspace")
    )
    assert git(repo, "worktree", "list", "--porcelain") == registration
    assert (candidate.git_dir / "switchstand-work-id").read_bytes() == metadata

    (candidate.git_dir / "switchstand-work-id").write_text(str(UUID(int=2)) + "\n")
    with pytest.raises(WorkspaceUnknown, match="metadata does not match"):
        inspect_candidate(repo, WORK_ID, state_home=state)
    assert git(repo, "worktree", "list", "--porcelain") == registration


def test_inspect_candidate_distinguishes_absent_from_missing_contents(tmp_path: Path) -> None:
    repo, base = repository(tmp_path / "repo")
    state = tmp_path / "state"
    with pytest.raises(WorkspaceAbsent):
        inspect_candidate(repo, WORK_ID, state_home=state)
    assert not state.exists()

    candidate = prepare_candidate(repo, WORK_ID, base, state_home=state)
    git(repo, "worktree", "unlock", str(candidate.path))
    git(repo, "worktree", "remove", "--force", str(candidate.path))
    with pytest.raises(WorkspaceMissing):
        inspect_candidate(repo, WORK_ID, state_home=state)
