from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import time
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


def dispatch_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    home = tmp_path / "home"
    primary = home / "switchstand"
    primary.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", primary, "init", "-b", "main"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", primary, "-c", "user.name=Test",
         "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "base"],
        check=True, capture_output=True,
    )
    codex_home = home / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}\n")
    (codex_home / "config.toml").write_text("")
    marker = tmp_path / "codex-executed"
    executable(
        codex_home / "packages/standalone/current/bin/codex",
        '#!/bin/sh\nprintf "executed\\n" > "$MARKER"\n',
    )
    executable(home / ".local/bin/codex", "#!/bin/sh\nexit 99\n")
    return home, primary, marker, os.environ | {
        "HOME": str(home), "MARKER": str(marker)
    }


def test_fresh_dispatch_can_commit_fetch_and_register_handoff(tmp_path: Path) -> None:
    home, primary, marker, env = dispatch_fixture(tmp_path)
    handoff = primary / "scripts/coordinator-handoff"
    executable(handoff, (ROOT / "scripts/coordinator-handoff").read_text())
    control = primary / "scripts/switchstand-coordinator-control-mcp"
    executable(
        control,
        """#!/bin/sh
set -eu
head=$(git -C "$HOME/switchstand" rev-parse HEAD)
printf '{"status":"ready","effect":"not_sent","previous_sha":"%s","target_sha":"%s","resulting_sha":"%s","reason":"test-owner"}\\n' "$head" "$head" "$head"
""",
    )
    (primary / ".gitignore").write_text("friction.md\n")
    subprocess.run(
        [
            "git", "-C", primary, "add", "scripts/coordinator-handoff",
            "scripts/switchstand-coordinator-control-mcp", ".gitignore",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", primary, "-c", "user.name=Test", "-c",
         "user.email=test@example.com", "commit", "-m", "test launch controls"],
        check=True, capture_output=True,
    )
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
    subprocess.run(["git", "-C", primary, "remote", "add", "origin", remote], check=True)
    subprocess.run(
        ["git", "-C", primary, "push", "-u", "origin", "main"],
        check=True, capture_output=True,
    )
    executable(
        home / ".codex/packages/standalone/current/bin/codex",
        """#!/bin/sh
set -eu
git config user.name Test
git config user.email test@example.com
printf 'fresh launch\n' > fresh-launch.txt
git add fresh-launch.txt
git commit -m 'fresh launch commit' >/dev/null
git fetch origin
record=
for candidate in "$CODEX_HOME"/start-commit.*; do
    case "$candidate" in *.manifest.json) continue;; esac
    [ -f "$candidate" ] || continue
    record=$candidate
done
[ -n "$record" ]
obligations="$HOME/.local/state/switchstand/fresh-launch-obligations"
printf 'fresh launch complete\n' > "$obligations"
"$HOME/switchstand/scripts/coordinator-handoff" "$record" "$obligations" >/dev/null
printf 'writer=%s\nhead=%s\n' "$PWD" "$(git rev-parse HEAD)" > "$MARKER"
""",
    )

    result = subprocess.run(
        [DISPATCH], cwd=primary, env=env, text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    launched = dict(line.split("=", 1) for line in marker.read_text().splitlines())
    writer = Path(launched["writer"])
    assert subprocess.check_output(
        ["git", "-C", writer, "show", "HEAD:fresh-launch.txt"], text=True,
    ) == "fresh launch\n"
    assert launched["head"] == subprocess.check_output(
        ["git", "-C", writer, "rev-parse", "HEAD"], text=True,
    ).strip()
    assert subprocess.check_output(
        ["git", "-C", writer, "rev-parse", "FETCH_HEAD"], text=True,
    ).strip() == subprocess.check_output(
        ["git", "-C", primary, "rev-parse", "origin/main"], text=True,
    ).strip()
    coordinator_home = home / ".local/state/switchstand/codex/coordinator"
    pending = json.loads((coordinator_home / "pending-handoff.json").read_text())
    artifact = Path(pending["artifact"])
    assert json.loads((artifact / "handoff.json").read_text())["state"] == "AWAITING_SUCCESSOR"
    profile = tomllib.loads(next(
        coordinator_home.glob("switchstand-coordinator-*.config.toml")
    ).read_text())
    filesystem = profile["permissions"]["switchstand-coordinator"]["filesystem"]
    writer_git_dir = subprocess.check_output(
        ["git", "-C", writer, "rev-parse", "--absolute-git-dir"], text=True,
    ).strip()
    assert filesystem[writer_git_dir] == "write"


def test_dispatch_rejects_invalid_legacy_friction_store_before_codex(
    tmp_path: Path,
) -> None:
    for kind in ("directory", "symlink"):
        case = tmp_path / kind
        home, primary, marker, env = dispatch_fixture(case)
        state = home / ".local/state/switchstand"
        state.mkdir(parents=True)
        legacy = state / "friction.md"
        if kind == "directory":
            legacy.mkdir()
        else:
            target = case / "external-friction"
            target.write_text("external unchanged\n")
            legacy.symlink_to(target)

        result = subprocess.run(
            [DISPATCH], cwd=primary, env=env, text=True,
            capture_output=True, check=False,
        )

        assert result.returncode != 0
        assert "Coordinator legacy friction store is invalid" in result.stderr
        assert not marker.exists()
        assert legacy.is_dir() if kind == "directory" else legacy.is_symlink()
        assert not (primary / "friction.md").exists()


def test_dispatch_rejects_invalid_current_friction_paths_without_mutating_legacy(
    tmp_path: Path,
) -> None:
    cases = ("root-file", "root-symlink", "store-directory", "store-symlink")
    for kind in cases:
        case = tmp_path / kind
        home, primary, marker, env = dispatch_fixture(case)
        state = home / ".local/state/switchstand"
        state.mkdir(parents=True)
        legacy = state / "friction.md"
        legacy.write_text("legacy unchanged\n")
        binding = primary / "friction.md"
        binding.symlink_to(legacy)
        binding_before = os.readlink(binding)
        root = state / "friction"
        if kind == "root-file":
            root.write_text("invalid root\n")
            expected = "Coordinator friction root is invalid"
        elif kind == "root-symlink":
            target = case / "external-root"
            target.mkdir()
            root.symlink_to(target)
            expected = "Coordinator friction root is invalid"
        else:
            root.mkdir()
            store = root / "friction.md"
            if kind == "store-directory":
                store.mkdir()
            else:
                target = case / "external-current-friction"
                target.write_text("external unchanged\n")
                store.symlink_to(target)
            expected = "Coordinator friction store is invalid"

        result = subprocess.run(
            [DISPATCH], cwd=primary, env=env, text=True,
            capture_output=True, check=False,
        )

        assert result.returncode != 0
        assert expected in result.stderr
        assert not marker.exists()
        assert legacy.read_text() == "legacy unchanged\n"
        assert binding.is_symlink()
        assert os.readlink(binding) == binding_before


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
    pwd_file = tmp_path / "pwd"
    executable(
        codex_home / "packages/standalone/current/bin/codex",
        '#!/bin/sh\nprintf "%s\\n" "$CODEX_HOME" > "$RESULT"\n'
        'pwd > "$PWD_RESULT"\n'
        'printf "%s\\n" "$@" >> "$RESULT"\n',
    )
    executable(home / ".local/bin/codex", "#!/bin/sh\nexit 99\n")

    result = subprocess.run(
        [DISPATCH, "resume", "test-session"], cwd=primary,
        env=os.environ | {
            "HOME": str(home), "RESULT": str(result_file), "PWD_RESULT": str(pwd_file)
        },
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
    assert "-m" not in arguments
    assert arguments[arguments.index("--enable") + 1] == "hooks"
    assert "--dangerously-bypass-hook-trust" in arguments
    assert 'default_permissions="switchstand-coordinator"' in arguments
    assert arguments[-2:] == ["resume", "test-session"]
    writer = Path(pwd_file.read_text().strip())
    assert writer.parent == home / ".local/state/switchstand/worktrees"
    assert writer.name.startswith("switchstand-coordinator-")
    assert subprocess.check_output(
        ["git", "-C", writer, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        text=True,
    ).strip() == str(primary / ".git")
    assert subprocess.check_output(
        ["git", "-C", writer, "rev-parse", "HEAD"], text=True
    ).strip() == subprocess.check_output(
        ["git", "-C", primary, "rev-parse", "HEAD"], text=True
    ).strip()

    profile_path = next(coordinator_home.glob("switchstand-coordinator-*.config.toml"))
    profile = tomllib.loads(profile_path.read_text())
    filesystem = profile["permissions"]["switchstand-coordinator"]["filesystem"]
    writer_git_dir = Path(subprocess.check_output(
        ["git", "-C", writer, "rev-parse", "--absolute-git-dir"], text=True,
    ).strip())
    assert filesystem[str(primary)] == {".": "read", ".git": "write"}
    assert filesystem[str(writer)] == "write"
    assert filesystem[str(writer_git_dir)] == "write"
    assert profile["approval_policy"] == "never"
    assert profile["features"]["multi_agent"] is True
    records = [path for path in coordinator_home.glob("start-commit.*")
               if path.is_file() and not path.name.endswith(".manifest.json")]
    assert len(records) == 1
    assert records[0].read_text() == subprocess.check_output(
        ["git", "-C", primary, "rev-parse", "HEAD"], text=True
    )
    assert str(records[0]) in profile["developer_instructions"]
    assert "synchronous compact-session hook" in profile["developer_instructions"]
    manifests = list(coordinator_home.glob("start-commit.*.manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text())
    assert manifest["session"]["start_commit"] == records[0].read_text().strip()
    assert manifest["session"]["writer"] == str(writer)
    runtime_receipt = manifest["runtime_mutable_controls"][0]
    snapshot = Path(runtime_receipt["snapshot"])
    assert snapshot.read_bytes() == profile_path.read_bytes()
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == runtime_receipt["launch_sha256"]
    hook_command = profile["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    hook_arguments = hook_command.split()
    assert hook_arguments[0] == str(ROOT / "scripts/codex-hook")
    assert hook_arguments[-2:] == ["--coordinator-writer", str(writer)]
    assert profile["mcp_servers"]["switchstand_coordinator_control"]["command"] == str(
        primary / "scripts/switchstand-coordinator-control-mcp"
    )
    assert "mode=PILOT; lifetime=ASSIGNMENT" in profile["developer_instructions"]
    assert set(profile["hooks"]) == {
        "PreToolUse", "SessionStart", "UserPromptSubmit", "Stop"
    }
    assert "SubagentStop" not in profile["hooks"]
    assert not (coordinator_home / "AGENTS.md").exists()
    friction_store = home / ".local/state/switchstand/friction.md"
    friction_root = home / ".local/state/switchstand/friction"
    current_friction_store = friction_root / "friction.md"
    assert (primary / "friction.md").is_symlink()
    assert (primary / "friction.md").resolve() == current_friction_store
    assert friction_store.read_text() == "existing friction\n"
    assert friction_store.stat().st_mode & 0o777 == 0o600
    assert friction_root.stat().st_mode & 0o777 == 0o700
    assert current_friction_store.read_text() == "existing friction\n"
    assert current_friction_store.stat().st_mode & 0o777 == 0o600
    assert (writer / "friction.md").resolve() == current_friction_store
    assert filesystem[str(friction_root)] == "write"
    assert str(friction_store) not in filesystem

    artifact = home / ".local/state/switchstand/codex/handoffs/handoff-real-successor"
    artifact.mkdir(parents=True)
    (artifact / "obligations").write_text("continue\n")
    git_head = subprocess.check_output(
        ["git", "-C", primary, "rev-parse", "HEAD"], text=True).strip()
    (artifact / "handoff.json").write_text(json.dumps({
        "handoff_id": "test-handoff", "state": "AWAITING_SUCCESSOR",
        "target_commit": git_head,
        "obligations_sha256": hashlib.sha256((artifact / "obligations").read_bytes()).hexdigest(),
    }))
    (coordinator_home / "pending-handoff.json").write_text(json.dumps(
        {"handoff_id": "test-handoff", "artifact": str(artifact)}
    ))
    fresh = subprocess.run(
        [DISPATCH], cwd=primary,
        env=os.environ | {
            "HOME": str(home), "RESULT": str(result_file), "PWD_RESULT": str(pwd_file)
        },
        text=True, capture_output=True, check=False,
    )
    assert fresh.returncode == 0, fresh.stderr
    manifests = [
        json.loads(path.read_text()) for path in coordinator_home.glob("*.manifest.json")
    ]
    assert len(manifests) == 2
    assert all("handoff" not in manifest for manifest in manifests)
    assert not (artifact / "successor-launch.json").exists()
    assert json.loads((coordinator_home / "pending-handoff.json").read_text()) == {
        "handoff_id": "test-handoff", "artifact": str(artifact)
    }

    repeated = subprocess.run(
        [DISPATCH, "resume", "test-session"], cwd=primary,
        env=os.environ | {
            "HOME": str(home), "RESULT": str(result_file), "PWD_RESULT": str(pwd_file)
        },
        text=True, capture_output=True, check=False,
    )
    assert repeated.returncode == 0, repeated.stderr
    assert friction_store.read_text() == "existing friction\n"
    assert current_friction_store.read_text() == "existing friction\n"
    profiles = sorted(coordinator_home.glob("switchstand-coordinator-*.config.toml"))
    assert len(profiles) == 3
    assert len({path.read_bytes() for path in profiles}) == 3
    for manifest_path in coordinator_home.glob("start-commit.*.manifest.json"):
        manifest = json.loads(manifest_path.read_text())
        recorded = next(
            item for item in manifest["frozen_controls"]
            if item["id"] == "generated:profile-snapshot"
        )
        generated = Path(recorded["path"])
        assert hashlib.sha256(generated.read_bytes()).hexdigest() == recorded["sha256"]
        receipt = manifest["runtime_mutable_controls"][0]
        assert Path(receipt["path"]) in profiles
        assert receipt["snapshot"] == str(generated)


def test_dispatch_preserves_explicit_off_control_and_scrubs_selectors(tmp_path: Path) -> None:
    home = tmp_path / "home"
    primary = home / "switchstand"
    primary.mkdir(parents=True)
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
    (codex_home / "config.toml").write_text("")
    result_file = tmp_path / "result"
    executable(
        codex_home / "packages/standalone/current/bin/codex",
        '#!/bin/sh\nprintf "%s|%s\n" "${SWITCHSTAND_CODEX_CONTINUITY-unset}" '
        '"${SWITCHSTAND_CODEX_LIFETIME-unset}" > "$RESULT"\n',
    )
    executable(home / ".local/bin/codex", "#!/bin/sh\nexit 99\n")

    result = subprocess.run(
        [DISPATCH], cwd=primary,
        env=os.environ | {
            "HOME": str(home), "RESULT": str(result_file),
            "SWITCHSTAND_CODEX_CONTINUITY": "OFF",
            "SWITCHSTAND_CODEX_LIFETIME": "ASSIGNMENT",
        },
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result_file.read_text() == "unset|unset\n"
    coordinator_home = home / ".local/state/switchstand/codex/coordinator"
    profile_path = next(coordinator_home.glob("switchstand-coordinator-*.config.toml"))
    profile = tomllib.loads(profile_path.read_text())
    assert "mode=OFF; lifetime=ASSIGNMENT" in profile["developer_instructions"]
    assert set(profile["hooks"]) == {"PreToolUse", "SessionStart"}


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


def test_concurrent_launch_keeps_first_profile_immutable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    primary = home / "switchstand"
    primary.mkdir(parents=True)
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
    (codex_home / "config.toml").write_text("")
    executable(
        codex_home / "packages/standalone/current/bin/codex",
        "#!/bin/sh\nsleep 1\n",
    )
    executable(home / ".local/bin/codex", "#!/bin/sh\nexit 99\n")

    first = subprocess.Popen(
        [DISPATCH, "resume", "session-a"], cwd=primary,
        env=os.environ | {"HOME": str(home)},
    )
    coordinator_home = home / ".local/state/switchstand/codex/coordinator"
    profiles: list[Path] = []
    for _ in range(200):
        profiles = list(coordinator_home.glob("switchstand-coordinator-*.config.toml"))
        if profiles:
            break
        time.sleep(0.01)
    assert len(profiles) == 1
    first_profile = profiles[0]
    first_bytes = first_profile.read_bytes()
    shared_hooks = coordinator_home / "hooks.json"
    shared_bytes = shared_hooks.read_bytes()

    second = subprocess.Popen(
        [DISPATCH, "resume", "session-b"], cwd=primary,
        env=os.environ | {"HOME": str(home)},
    )
    assert first.wait(timeout=5) == 0
    assert second.wait(timeout=5) == 0
    assert first_profile.read_bytes() == first_bytes
    assert shared_hooks.read_bytes() == shared_bytes
    assert len(list(coordinator_home.glob("switchstand-coordinator-*.config.toml"))) == 2
    for manifest_path in coordinator_home.glob("start-commit.*.manifest.json"):
        manifest = json.loads(manifest_path.read_text())
        recorded = next(
            item for item in manifest["frozen_controls"]
            if item["id"] == "generated:profile-snapshot"
        )
        generated = Path(recorded["path"])
        assert hashlib.sha256(generated.read_bytes()).hexdigest() == recorded["sha256"]
        runtime = Path(manifest["runtime_mutable_controls"][0]["path"])
        assert generated.read_bytes() == runtime.read_bytes()
        profile = tomllib.loads(generated.read_text())
        assert str(manifest["session"]["writer"]) in (
            profile["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        )
