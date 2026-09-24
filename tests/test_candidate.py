import shutil
import subprocess
from dataclasses import fields
from pathlib import Path
from uuid import UUID

import pytest

from switchstand import candidate as candidate_module
from switchstand.candidate import (
    CandidateError,
    CandidateIdentity,
    WorkspaceAbsent,
    WorkspaceMissing,
    WorkspaceUnknown,
    WriterBusy,
    inspect_candidate,
    prepare_candidate,
    prepare_launch_source,
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


def completed(*, stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], returncode, stdout, "")


def test_launch_source_remote_movement_fails_before_local_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(candidate_module, "_remote_ref_sha", lambda _repo, _ref: "c" * 40)

    def no_local_ref(_repo: Path, _name: str) -> str | None:
        raise AssertionError("local launch ref must not be inspected after remote movement")

    monkeypatch.setattr(candidate_module, "_local_ref_sha", no_local_ref)
    with pytest.raises(CandidateError, match="launch candidate moved"):
        candidate_module._prepare_remote_ref(
            Path("."), "123", "candidate", "refs/pull/37/head", "b" * 40
        )


def test_launch_local_ref_uses_real_git_missing_ref_exit_code(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    name = "refs/switchstand/launch/123/base"
    assert candidate_module._local_ref_sha(tmp_path, name) is None

    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=Test",
         "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "fixture"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(tmp_path), "update-ref", name, sha], check=True)
    assert candidate_module._local_ref_sha(tmp_path, name) == sha


def test_launch_remote_ref_fetches_on_first_use_with_real_git(tmp_path: Path) -> None:
    remote = tmp_path / "remote"
    control = tmp_path / "control"
    subprocess.run(["git", "init", "-q", "-b", "main", str(remote)], check=True)
    subprocess.run(
        ["git", "-C", str(remote), "-c", "user.name=Test",
         "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "fixture"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "init", "-q", str(control)], check=True)
    subprocess.run(["git", "-C", str(control), "remote", "add", "origin", str(remote)], check=True)
    name = "refs/switchstand/launch/123/base"

    assert candidate_module._local_ref_sha(control, name) is None
    candidate_module._prepare_remote_ref(control, "123", "base", "refs/heads/main", sha)
    assert candidate_module._local_ref_sha(control, name) == sha


def test_launch_remote_ref_rejects_non_commit_object(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = "b" * 40
    monkeypatch.setattr(candidate_module, "_remote_ref_sha", lambda _repo, _ref: expected)
    monkeypatch.setattr(candidate_module, "_local_ref_sha", lambda _repo, _name: expected)

    def fake_git(_repo: Path, *arguments: str, check: bool = True):
        if arguments[:2] == ("cat-file", "-e"):
            return completed(returncode=1)
        raise AssertionError(arguments)

    monkeypatch.setattr(candidate_module, "_git", fake_git)
    with pytest.raises(CandidateError, match="not an available commit"):
        candidate_module._prepare_remote_ref(
            Path("."), "123", "candidate", "refs/pull/37/head", expected
        )


def test_prepare_launch_source_rejects_task_base_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_git(_repo: Path, *arguments: str, check: bool = True):
        del check
        if arguments == ("rev-parse", "HEAD"):
            return completed(stdout="c" * 40 + "\n")
        raise AssertionError(arguments)

    monkeypatch.setattr(candidate_module, "_git", fake_git)
    monkeypatch.setattr(
        candidate_module, "_prepare_remote_ref",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must fail before fetch")),
    )
    with pytest.raises(CandidateError, match="task base does not match"):
        prepare_launch_source(
            Path("."), "123", repository="marcogallotta/switchstand",
            base_ref="refs/heads/main", base_sha="a" * 40,
            candidate_ref="refs/pull/37/head", candidate_sha="b" * 40,
            control_sha="c" * 40,
        )


def test_prepare_launch_source_rejects_wrong_control_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        candidate_module, "_git",
        lambda _repo, *arguments, **_kwargs: completed(stdout="c" * 40 + "\n")
        if arguments == ("rev-parse", "HEAD") else (_ for _ in ()).throw(AssertionError(arguments)),
    )
    with pytest.raises(CandidateError, match="not executing from the selected CONTROL SHA"):
        prepare_launch_source(
            Path("."), "123", repository="marcogallotta/switchstand",
            base_ref="refs/heads/main", base_sha="a" * 40,
            candidate_ref="refs/pull/37/head", candidate_sha="b" * 40,
            control_sha="a" * 40,
        )


def test_prepare_launch_source_uses_selected_control_and_exact_remote_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, head = "a" * 40, "b" * 40

    def fake_git(_repo: Path, *arguments: str, check: bool = True):
        del check
        if arguments == ("rev-parse", "HEAD"):
            return completed(stdout=base + "\n")
        if arguments == ("remote", "get-url", "origin"):
            return completed(stdout="https://github.com/marcogallotta/switchstand.git\n")
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            return completed()
        raise AssertionError(arguments)

    fetched: list[tuple[str, str]] = []
    monkeypatch.setattr(candidate_module, "_git", fake_git)
    monkeypatch.setattr(
        candidate_module, "_prepare_remote_ref",
        lambda _repo, _task, label, _ref, sha: fetched.append((label, sha)),
    )
    prepare_launch_source(
        Path("."), "123", repository="marcogallotta/switchstand",
        base_ref="refs/heads/main", base_sha=base,
        candidate_ref="refs/pull/37/head", candidate_sha=head,
        control_sha=base,
    )
    assert fetched == [("base", base), ("candidate", head)]
