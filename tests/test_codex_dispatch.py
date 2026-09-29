from __future__ import annotations

import json
import os
import stat
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
DISPATCH = ROOT / "scripts/codex-dispatch"


def executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def poisoned_git_environment(primary: Path) -> dict[str, str]:
    return {
        "GIT_DIR": str(primary / ".git"),
        "GIT_WORK_TREE": str(primary),
        "GIT_COMMON_DIR": str(primary / ".git"),
        "GIT_CEILING_DIRECTORIES": str(primary),
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "true",
    }


def test_dispatch_uses_promptless_primary_fence_without_global_instructions(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    primary = home / "switchstand"
    primary.mkdir(parents=True)
    (primary / "friction.md").write_text("existing friction\n")
    subprocess.run(["git", "-C", primary, "init", "-b", "main"], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", primary, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "--allow-empty", "-m", "base"],
        check=True, capture_output=True,
    )
    codex_home = home / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}\n")
    (codex_home / "AGENTS.md").write_text("must remain outside coordinator home\n")
    (codex_home / "config.toml").write_text(
        'model_auto_compact_token_limit = 200000\n'
        'approval_policy = "on-request"\n'
    )
    result_file = tmp_path / "result"
    executable(
        codex_home / "packages/standalone/current/bin/codex",
        '#!/bin/sh\nprintf "%s\\n" "$CODEX_HOME" > "$RESULT"\n'
        'printf "%s\\n" "$@" >> "$RESULT"\n',
    )

    result = subprocess.run(
        [DISPATCH, "resume", "test-session"], cwd=primary,
        env=os.environ | {"HOME": str(home), "RESULT": str(result_file)},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = result_file.read_text().splitlines()
    coordinator_home = home / ".local/state/switchstand/codex/coordinator"
    assert lines[0] == str(coordinator_home)
    arguments = lines[1:]
    assert "--dangerously-bypass-approvals-and-sandbox" not in arguments
    assert "--disable" not in arguments
    assert arguments[arguments.index("-a") + 1] == "never"
    assert arguments[arguments.index("--enable") + 1] == "hooks"
    assert "--dangerously-bypass-hook-trust" in arguments
    assert 'default_permissions="switchstand-coordinator"' in arguments
    assert arguments[-2:] == ["resume", "test-session"]

    profile = tomllib.loads(
        (coordinator_home / "switchstand-coordinator-preferences.config.toml").read_text()
    )
    filesystem = profile["permissions"]["switchstand-coordinator"]["filesystem"]
    assert filesystem[str(primary)] == {".": "read", ".git": "write"}
    assert profile["approval_policy"] == "never"
    records = list(coordinator_home.glob("start-commit.*"))
    assert len(records) == 1
    assert records[0].read_text() == subprocess.check_output(
        ["git", "-C", primary, "rev-parse", "HEAD"], text=True
    )
    assert str(records[0]) in profile["developer_instructions"]
    assert "after context compaction" in profile["developer_instructions"]
    hooks = json.loads((coordinator_home / "hooks.json").read_text())
    assert set(hooks["hooks"]) == {"PreToolUse"}
    assert not (coordinator_home / "AGENTS.md").exists()
    friction_store = home / ".local/state/switchstand/friction.md"
    assert (primary / "friction.md").is_symlink()
    assert (primary / "friction.md").resolve() == friction_store
    assert friction_store.read_text() == "existing friction\n"
    assert friction_store.stat().st_mode & 0o777 == 0o600

    repeated = subprocess.run(
        [DISPATCH, "resume", "test-session"], cwd=primary,
        env=os.environ | {"HOME": str(home), "RESULT": str(result_file)},
        text=True, capture_output=True, check=False,
    )
    assert repeated.returncode == 0, repeated.stderr
    assert friction_store.read_text() == "existing friction\n"


def test_dispatch_outside_repo_ignores_ambient_git_repository_selection(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    primary = home / "switchstand"
    primary.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", primary, "init", "-b", "main"],
        check=True, capture_output=True,
    )
    result_file = tmp_path / "result"
    executable(
        home / ".codex/packages/standalone/current/bin/codex",
        '#!/bin/sh\nprintf "real\\n%s\\n" "$*" > "$RESULT"\n',
    )
    outside = home / "outside"
    outside.mkdir()

    result = subprocess.run(
        [DISPATCH, "exec", "outside"], cwd=outside,
        env=os.environ | {
            "HOME": str(home),
            "RESULT": str(result_file),
        } | poisoned_git_environment(primary),
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result_file.read_text().splitlines() == ["real", "exec outside"]
    assert not (home / ".local/state/switchstand/codex/coordinator").exists()
