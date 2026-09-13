import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from switchstand.candidate import WorkspaceMissing, WorkspaceUnknown, WriterBusy, prepare_candidate

WORK_ID = UUID("00000000-0000-0000-0000-000000000001")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()


def repository(path: Path, origin: str = "https://example.test/switchstand.git") -> tuple[Path, str]:
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
