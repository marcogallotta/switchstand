import shutil
import subprocess
from pathlib import Path

import pytest

from switchstand.git import GitError, reconcile


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repo, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    if shutil.which("git") is None:
        pytest.skip("git is required for real repository reconciliation tests")
    supported = subprocess.run(
        ["git", "--no-lazy-fetch", "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    if supported.returncode != 0:
        pytest.skip("git with --no-lazy-fetch is required for reconciliation tests")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "switchstand@example.invalid")
    _git(repo, "config", "user.name", "Switchstand Test")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _candidate(repo: Path, base: str, branch: str = "candidate") -> str:
    _git(repo, "switch", "-c", branch, base)
    (repo / "candidate.txt").write_text("reviewed candidate\n")
    _git(repo, "add", "candidate.txt")
    _git(repo, "commit", "-m", "candidate")
    return _git(repo, "rev-parse", "HEAD")


def _merge(repo: Path, candidate: str, branch: str = "main") -> str:
    _git(repo, "switch", branch)
    _git(repo, "merge", "--no-ff", candidate, "-m", "merge candidate")
    return _git(repo, "rev-parse", "HEAD")


def test_reconcile_accepts_current_base_reviewed_two_parent_merge(tmp_path: Path):
    repo, base = _repo(tmp_path)
    candidate = _candidate(repo, base)
    merged = _merge(repo, candidate)

    assert _git(repo, "rev-list", "--parents", "-n", "1", merged).split() == [
        merged, base, candidate,
    ]
    assert reconcile(repo, candidate, base, merged, merged).tree == _git(
        repo, "rev-parse", f"{candidate}^{{tree}}")


def test_reconcile_rejects_squash_landing(tmp_path: Path):
    repo, base = _repo(tmp_path)
    candidate = _candidate(repo, base)
    _git(repo, "switch", "main")
    _git(repo, "merge", "--squash", candidate)
    _git(repo, "commit", "-m", "squash candidate")
    merged = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(GitError, match="reviewed base"):
        reconcile(repo, candidate, base, merged, merged)


def test_reconcile_rejects_stale_base(tmp_path: Path):
    repo, base = _repo(tmp_path)
    candidate = _candidate(repo, base)
    _git(repo, "switch", "main")
    _git(repo, "commit", "--allow-empty", "-m", "main advanced")
    merged = _merge(repo, candidate)

    with pytest.raises(GitError, match="reviewed base"):
        reconcile(repo, candidate, base, merged, merged)


def test_reconcile_rejects_wrong_tree(tmp_path: Path):
    repo, base = _repo(tmp_path)
    candidate = _candidate(repo, base)
    _git(repo, "switch", "main")
    _git(repo, "read-tree", candidate)
    (repo / "candidate.txt").write_text("wrong tree\n")
    _git(repo, "add", "candidate.txt")
    tree = _git(repo, "write-tree")
    committed = subprocess.run(
        ["git", "commit-tree", tree, "-p", base, "-p", candidate],
        cwd=repo, text=True, input="tampered merge\n", capture_output=True, check=True,
    )
    merged = committed.stdout.strip()
    _git(repo, "update-ref", "refs/heads/main", merged)
    _git(repo, "reset", "--hard", merged)

    with pytest.raises(GitError, match="landing tree"):
        reconcile(repo, candidate, base, merged, merged)


def test_reconcile_rejects_unrelated_head_with_matching_tree(tmp_path: Path):
    repo, base = _repo(tmp_path)
    _git(repo, "switch", "--orphan", "unrelated")
    (repo / "candidate.txt").write_text("reviewed candidate\n")
    _git(repo, "add", "candidate.txt")
    _git(repo, "commit", "-m", "unrelated candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "main")
    _git(repo, "merge", "--no-ff", "--allow-unrelated-histories", candidate, "-m",
         "merge unrelated candidate")
    merged = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(GitError, match="not based"):
        reconcile(repo, candidate, base, merged, merged)


def test_reconcile_requires_exact_commit_identity(tmp_path: Path):
    with pytest.raises(GitError, match="exact 40-character SHA"):
        reconcile(tmp_path, "HEAD", "a" * 40, "b" * 40, "b" * 40)
