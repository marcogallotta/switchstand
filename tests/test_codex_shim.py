from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
INSTALLER = ROOT / "scripts" / "install-codex-shim"


def executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def install(home: Path) -> Path:
    result = subprocess.run(
        [INSTALLER], env=os.environ | {"HOME": str(home)},
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return home / ".local/bin/codex"


@pytest.mark.parametrize("checkout_state", ["missing", "broken"])
def test_outside_repo_launch_bypasses_missing_or_broken_checkout(
    tmp_path: Path, checkout_state: str,
) -> None:
    home = tmp_path / "home"
    result_file = tmp_path / "result"
    executable(
        home / ".codex/packages/standalone/current/bin/codex",
        '#!/bin/sh\nprintf "real\\n%s\\n" "$*" > "$RESULT"\n',
    )
    if checkout_state == "broken":
        (home / "switchstand").mkdir(parents=True)
        (home / "switchstand/.git").write_text("not a git directory\n")
    launcher = install(home)
    outside = home / "outside"
    outside.mkdir()

    result = subprocess.run(
        [launcher, "exec", "hello"], cwd=outside,
        env=os.environ | {"HOME": str(home), "RESULT": str(result_file)},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result_file.read_text().splitlines() == ["real", "exec hello"]


def test_inside_canonical_git_common_delegates_to_repo_dispatcher(tmp_path: Path) -> None:
    home = tmp_path / "home"
    primary = home / "switchstand"
    primary.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", primary, "init", "-b", "main"],
        check=True, capture_output=True,
    )
    git_env = os.environ | {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(
        ["git", "-C", primary, "commit", "--allow-empty", "-m", "initial"],
        env=git_env, check=True, capture_output=True,
    )
    result_file = tmp_path / "result"
    executable(
        primary / "scripts/codex-dispatch",
        '#!/bin/sh\nprintf "dispatcher\\n%s\\n" "$*" > "$RESULT"\n',
    )
    executable(
        home / ".codex/packages/standalone/current/bin/codex",
        '#!/bin/sh\nprintf "real\\n%s\\n" "$*" > "$RESULT"\n',
    )
    launcher = install(home)
    nested = primary / "nested"
    nested.mkdir()

    result = subprocess.run(
        [launcher, "resume", "test-session"], cwd=nested,
        env=os.environ | {"HOME": str(home), "RESULT": str(result_file)},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result_file.read_text().splitlines() == [
        "dispatcher", "resume test-session",
    ]

    writer = home / "writer"
    subprocess.run(
        ["git", "-C", primary, "worktree", "add", "-b", "writer", writer],
        env=git_env, check=True, capture_output=True,
    )
    result = subprocess.run(
        [launcher, "exec", "linked"], cwd=writer,
        env=os.environ | {"HOME": str(home), "RESULT": str(result_file)},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result_file.read_text().splitlines() == ["dispatcher", "exec linked"]


def test_installer_replaces_checkout_symlink_and_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    destination = home / ".local/bin/codex"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(ROOT / "scripts/codex-dispatch")

    launcher = install(home)
    first = launcher.stat()
    first_content = launcher.read_bytes()
    assert not launcher.is_symlink()
    assert first.st_mode & stat.S_IXUSR

    assert install(home) == launcher
    second = launcher.stat()
    assert second.st_ino == first.st_ino
    assert launcher.read_bytes() == first_content
