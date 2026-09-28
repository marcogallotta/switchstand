from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def launcher(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    home = tmp_path / "home"
    home.mkdir()
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", remote], check=True, capture_output=True)
    repo = home / "switchstand"
    subprocess.run(["git", "clone", remote, repo], check=True, capture_output=True)
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    scripts = repo / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "scripts" / "codex-dispatch", scripts / "codex-dispatch")
    (scripts / "codex-coordinator-profile").write_text("#!/bin/sh\nexit 0\n")
    (scripts / "codex-coordinator-profile").chmod(0o755)
    git(repo, "add", "scripts")
    git(repo, "commit", "-m", "Install launcher")
    git(repo, "push", "origin", "main")

    codex = home / ".codex" / "packages" / "standalone" / "current" / "bin" / "codex"
    codex.parent.mkdir(parents=True)
    codex.write_text("#!/bin/sh\necho launched\n")
    codex.chmod(0o755)
    (home / ".codex" / "auth.json").write_text("test authentication\n")
    env = dict(os.environ, HOME=str(home), GIT_TERMINAL_PROMPT="0")
    return repo, remote, env


def launch(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [repo / "scripts" / "codex-dispatch"], cwd=repo, env=env, text=True, capture_output=True
    )


def test_clean_current_main_launches_coordinator(launcher: tuple[Path, Path, dict[str, str]]) -> None:
    repo, _, env = launcher

    result = launch(repo, env)

    assert result.returncode == 0
    assert result.stdout.strip() == "launched"
    assert (Path(env["HOME"]) / ".local/state/switchstand/codex/coordinator/auth.json").is_symlink()


@pytest.mark.parametrize("reason", ["stale", "dirty", "unverifiable"])
def test_control_failure_blocks_before_profile_or_codex(
    launcher: tuple[Path, Path, dict[str, str]], reason: str
) -> None:
    repo, _, env = launcher
    if reason == "stale":
        git(repo, "commit", "--allow-empty", "-m", "Unpublished local head")
    elif reason == "dirty":
        (repo / "untracked.txt").write_text("local change\n")
    else:
        git(repo, "remote", "remove", "origin")

    result = launch(repo, env)

    assert result.returncode != 0
    assert "STALE Coordinator control:" in result.stderr
    assert "launched" not in result.stdout
    assert not (Path(env["HOME"]) / ".local/state/switchstand/codex/coordinator").exists()


def test_old_linked_worktree_cannot_launch_coordinator(
    launcher: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, _, env = launcher
    old_head = git(repo, "rev-parse", "HEAD")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "old", str(linked), old_head)
    git(repo, "commit", "--allow-empty", "-m", "Advance main")
    git(repo, "push", "origin", "main")

    result = launch(linked, env)

    assert result.returncode != 0
    assert "STALE Coordinator control:" in result.stderr
    assert "launched" not in result.stdout


def test_outside_canonical_repo_keeps_plain_codex_route(
    launcher: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, _, env = launcher
    outside = tmp_path / "outside"
    outside.mkdir()
    result = subprocess.run(
        [repo / "scripts" / "codex-dispatch"], cwd=outside, env=env, text=True, capture_output=True
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "launched"
