from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
INSTALLER = ROOT / "scripts" / "install-claude-shim"


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
    return home / ".local/bin/claude"


def poisoned_git_environment(primary: Path) -> dict[str, str]:
    return {
        "GIT_DIR": str(primary / ".git"),
        "GIT_WORK_TREE": str(primary),
        "GIT_COMMON_DIR": str(primary / ".git"),
        "GIT_CEILING_DIRECTORIES": str(primary),
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "true",
    }


@pytest.mark.parametrize("checkout_state", ["missing", "broken"])
def test_outside_repo_launch_bypasses_missing_or_broken_checkout(
    tmp_path: Path, checkout_state: str,
) -> None:
    home = tmp_path / "home"
    result_file = tmp_path / "result"
    executable(
        home / ".local/share/claude/versions/2.1.284",
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
        primary / "scripts/claude-dispatch",
        '#!/bin/sh\nprintf "dispatcher\\n%s\\n" "$*" > "$RESULT"\n',
    )
    executable(
        home / ".local/share/claude/versions/2.1.284",
        '#!/bin/sh\nprintf "real\\n%s\\n" "$*" > "$RESULT"\n',
    )
    launcher = install(home)
    nested = primary / "nested"
    nested.mkdir()

    result = subprocess.run(
        [launcher, "resume", "test-session"], cwd=nested,
        env=os.environ | {
            "HOME": str(home),
            "RESULT": str(result_file),
            "GIT_CEILING_DIRECTORIES": str(nested),
            "GIT_DISCOVERY_ACROSS_FILESYSTEM": "false",
        },
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


def test_linked_writer_launch_uses_current_canonical_memory_hook(tmp_path: Path) -> None:
    home = tmp_path / "home"
    primary = home / "switchstand"
    writer = home / "old-writer"
    primary.mkdir(parents=True)
    subprocess.run(["git", "-C", primary, "init", "-b", "main"],
                   check=True, capture_output=True)
    executable(primary / "scripts/codex-hook", "#!/bin/sh\nexit 0\n")
    git_env = os.environ | {
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(["git", "-C", primary, "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", primary, "commit", "-m", "old hook"],
                   env=git_env, check=True, capture_output=True)
    subprocess.run(["git", "-C", primary, "worktree", "add", "-b", "old", writer],
                   env=git_env, check=True, capture_output=True)

    executable(primary / "scripts/codex-hook", (ROOT / "scripts/codex-hook").read_text())
    executable(primary / "scripts/claude-dispatch", (ROOT / "scripts/claude-dispatch").read_text())
    settings = primary / ".claude/coordinator-settings.json"
    settings.parent.mkdir()
    settings.write_text((ROOT / ".claude/coordinator-settings.json").read_text())
    executable(home / ".local/share/claude/versions/2.1.284", """#!/usr/bin/env python3
import json, os, subprocess, sys
settings = json.load(open(sys.argv[sys.argv.index('--settings') + 1]))
command = settings['hooks']['PreToolUse'][0]['hooks'][0]['command']
payload = {'hook_event_name': 'PreToolUse', 'tool_name': os.environ['HOOK_TOOL'],
           'tool_input': {os.environ['HOOK_FIELD']: os.environ['HOOK_PATH']},
           'cwd': os.getcwd()}
result = subprocess.run(command, shell=True, input=json.dumps(payload),
                        text=True, capture_output=True)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
sys.exit(result.returncode)
""")

    def launch(tool: str, field: str, path: Path) -> dict:
        result = subprocess.run(
            [primary / "scripts/claude-dispatch"], cwd=writer,
            env=os.environ | {"HOME": str(home), "CLAUDE_PROJECT_DIR": str(writer),
                              "HOOK_TOOL": tool, "HOOK_FIELD": field,
                              "HOOK_PATH": str(path)},
            text=True, capture_output=True, check=True,
        )
        return json.loads(result.stdout) if result.stdout else {}

    memory = home / ".claude/projects/p/memory"
    for tool, field, name in (("Write", "file_path", "MEMORY.md"),
                              ("NotebookEdit", "notebook_path", "notes.ipynb")):
        denied = launch(tool, field, memory / name)
        assert "memory-write" in denied["hookSpecificOutput"]["permissionDecisionReason"]
        assert "work state" not in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert launch("Write", "file_path", home / ".claude/projects/p/settings.json") == {}


def test_outside_repo_ignores_ambient_git_repository_selection(tmp_path: Path) -> None:
    home = tmp_path / "home"
    primary = home / "switchstand"
    primary.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", primary, "init", "-b", "main"],
        check=True, capture_output=True,
    )
    result_file = tmp_path / "result"
    executable(
        primary / "scripts/claude-dispatch",
        '#!/bin/sh\nprintf "dispatcher\\n" > "$RESULT"\n',
    )
    executable(
        home / ".local/share/claude/versions/2.1.284",
        '#!/bin/sh\nprintf "real\\n%s\\n" "$*" > "$RESULT"\n',
    )
    launcher = install(home)
    outside = home / "outside"
    outside.mkdir()

    result = subprocess.run(
        [launcher, "exec", "outside"], cwd=outside,
        env=os.environ | {
            "HOME": str(home),
            "RESULT": str(result_file),
        } | poisoned_git_environment(primary),
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result_file.read_text().splitlines() == ["real", "exec outside"]


def test_installer_replaces_checkout_symlink_and_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    destination = home / ".local/bin/claude"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(ROOT / "scripts/claude-dispatch")

    launcher = install(home)
    first = launcher.stat()
    first_content = launcher.read_bytes()
    assert not launcher.is_symlink()
    assert first.st_mode & stat.S_IXUSR

    assert install(home) == launcher
    second = launcher.stat()
    assert second.st_ino == first.st_ino
    assert launcher.read_bytes() == first_content
