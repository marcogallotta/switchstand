from __future__ import annotations

import json
import os
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "codex-coordinator-profile"


def prepare(source: Path, destination: Path, primary: Path, hooks: Path) -> Path:
    hook = primary / "scripts/codex-hook"
    continuity_hook = primary / "scripts/codex-continuity-hook"
    compact_hook = primary / "scripts/codex-compact-hook"
    coordinator_control = primary / "scripts/coordinator-control"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    continuity_hook.write_text("#!/bin/sh\nexit 0\n")
    continuity_hook.chmod(continuity_hook.stat().st_mode | stat.S_IXUSR)
    compact_hook.write_text("#!/bin/sh\nexit 0\n")
    compact_hook.chmod(compact_hook.stat().st_mode | stat.S_IXUSR)
    coordinator_control.write_text("#!/bin/sh\nexit 0\n")
    coordinator_control.chmod(coordinator_control.stat().st_mode | stat.S_IXUSR)
    inspector = primary / "scripts/codex-worker-inspect"
    inspector.write_text("#!/bin/sh\nexit 0\n")
    inspector.chmod(inspector.stat().st_mode | stat.S_IXUSR)
    runtime_profile = destination.with_name(f"{destination.stem}.runtime.toml")
    writer = primary.parent / "writer"
    writer.mkdir(exist_ok=True)
    writer_git_dir = primary / ".git/worktrees/writer"
    writer_git_dir.mkdir(parents=True, exist_ok=True)
    (writer / ".git").write_text(f"gitdir: {writer_git_dir}\n")
    state = primary.parent / "coordinator"
    start_record = state / f"start-commit.{destination.stem}"
    manifest_record = state / f"launch-manifest.{destination.stem}.json"
    subprocess.run(
        [SCRIPT, source, destination, runtime_profile, primary, writer, hooks, hook, continuity_hook,
         compact_hook, coordinator_control,
         "OFF", "ASSIGNMENT", state / "telemetry.jsonl", start_record, manifest_record],
        check=True,
    )
    return runtime_profile


def test_coordinator_control_mcp_launcher_is_executable() -> None:
    launcher = ROOT / "scripts/switchstand-coordinator-control-mcp"
    assert os.access(launcher, os.X_OK), "Codex spawns this MCP command directly; it must be executable"


def test_profile_copies_only_benign_user_preferences(tmp_path: Path) -> None:
    source = tmp_path / "config.toml"
    destination = tmp_path / "coordinator.config.toml"
    primary = tmp_path / "primary"
    primary.mkdir()
    hooks = tmp_path / "hooks.json"
    source.write_text(
        """
model_auto_compact_token_limit = 200000
model_auto_compact_token_limit_scope = "total"
sandbox_mode = "danger-full-access"
approval_policy = "on-request"

[tui]
alternate_screen = "never"

[notice]
hide_rate_limit_model_nudge = true

[features]
multi_agent = false

[shell_environment_policy]
set = { SECRET = "must-not-copy" }

[projects."/tmp"]
trust_level = "trusted"

[apps.dish.tools.write]
approval_mode = "approve"

[mcp_servers.unrelated]
url = "https://example.invalid/mcp"

[hooks.state]
trusted_hash = "must-not-copy"
""".lstrip()
    )

    runtime_profile = prepare(source, destination, primary, hooks)

    profile = tomllib.loads(destination.read_text())
    assert profile["approval_policy"] == "never"
    assert profile["sandbox_mode"] == "danger-full-access"
    instructions = profile["developer_instructions"]
    assert str(tmp_path / "coordinator/start-commit.coordinator.config") in instructions
    assert "synchronous compact-session hook" in instructions
    frozen_control = (
        tmp_path / "coordinator/start-commit.coordinator.config.compact-controls/coordinator-control"
    )
    manifest = tmp_path / "coordinator/launch-manifest.coordinator.config.json"
    assert f"{frozen_control} check {manifest} --trigger post-compaction" in instructions
    assert f"{frozen_control} check {manifest} --trigger post-sync" in instructions
    assert "Never substitute writer-relative scripts/coordinator-control" in instructions
    assert f"owned linked writer is {primary.parent / 'writer'}" in instructions
    assert "Changed launch controls require only the reported affected-boundary recheck" in instructions
    assert "agent_takeover" not in instructions
    assert "agent_register" not in instructions
    assert "agent_transfer_request" not in instructions
    assert (
        "This raw Coordinator launch is ordinary and unbound: never call "
        'work_get(api_version="1") without a WorkId to infer current focus.'
    ) in instructions
    assert (
        "Ground work through an exact WorkId from Marco's direct assignment or an acknowledged "
        "addressed handoff, and call work_get with that exact ID. When Marco directly assigns new "
        "substantive work without one, resolve and read START HERE 1218327002478382 as its "
        "provider-neutral parent. If the parent exposes an exact canonical_root"
    ) in instructions
    assert "work_type=Task, lifecycle_state=CURRENT, owner_key=SELF" in instructions
    assert "wait_kind=unblock_condition=next_due=NONE" in instructions
    assert "otherwise use only the legacy create's required arguments" in instructions
    assert "Use one stable OperationId" in instructions
    assert "next_action_class=OWNER_CAN_DO" in instructions
    assert "continue only after exact work_get readback of the created WorkId" in instructions
    assert "only with the identical OperationId and arguments as directed" in instructions
    assert "never mint a second intent" in instructions
    assert "Do not create work for simple questions, status, corrections" in instructions
    assert "creation is denied/unresolved, report COVERAGE_GAP / UNKNOWN" in instructions
    assert "Bare work_get is reserved for trusted launch-bound managed runs." in instructions
    assert (
        "Built-in Workers are read-only because they share Root's filesystem profile"
        in instructions
    )
    assert "Native full local-user execution applies to directly operated Root" in instructions
    assert "grants no provider, deployment, activation" in instructions
    assert (
        f"`{primary / 'scripts/codex-worker-inspect'} --root {primary.parent / 'writer'}`"
        in instructions
    )
    assert "runtime denies every other shell and every file-edit call" in instructions
    assert "a Worker returns a patch proposal and Root applies it" in instructions
    assert not Path(
        f"{tmp_path / 'coordinator/start-commit.coordinator.config'}.worker-writers"
    ).exists()
    assert "mode=OFF; lifetime=ASSIGNMENT" in instructions
    assert "forked Workers" not in instructions
    assert profile["features"] == {"hooks": True, "multi_agent": True}
    canonical = tomllib.loads((ROOT / ".codex/config.toml").read_text())["mcp_servers"][
        "switchstand"
    ]
    assert profile["mcp_servers"] == {
        "switchstand": canonical | {"required": True},
        "switchstand_coordinator_control": {
            "command": str(primary / "scripts/switchstand-coordinator-control-mcp"),
            "required": True,
            "default_tools_approval_mode": "approve",
            "enabled_tools": [
                "coordinator_currentness_get", "coordinator_main_sync",
                "coordinator_implementation_spawn", "coordinator_implementation_status",
                "coordinator_implementation_cancel",
            ],
        }
    }
    assert "permissions" not in profile
    assert profile["model_auto_compact_token_limit"] == 200000
    assert profile["model_auto_compact_token_limit_scope"] == "total"
    assert profile["tui"] == {"alternate_screen": "never"}
    assert profile["notice"] == {"hide_rate_limit_model_nudge": True}
    assert set(profile) == {
        "approval_policy", "sandbox_mode", "developer_instructions", "features",
        "mcp_servers", "hooks",
        "model_auto_compact_token_limit", "model_auto_compact_token_limit_scope",
        "tui", "notice",
    }
    group = profile["hooks"]["PreToolUse"][0]
    assert group["hooks"][0]["command"] == (
        f"{primary / 'scripts/codex-hook'} --coordinator-primary {primary} "
        f"--coordinator-writer {primary.parent / 'writer'}"
    )
    compact = profile["hooks"]["SessionStart"][0]
    assert compact["matcher"] == "^compact$"
    assert compact["hooks"][0] == {
        "type": "command",
        "command": (
            f"{tmp_path / 'coordinator/start-commit.coordinator.config.compact-controls/codex-compact-hook'} "
            f"{tmp_path / 'coordinator/launch-manifest.coordinator.config.json'}"
        ),
        "timeout": 10,
        "additionalContextLimit": 1200,
    }
    assert set(profile["hooks"]) == {"PreToolUse", "SessionStart"}
    assert destination.stat().st_mode & 0o777 == 0o400
    assert runtime_profile.read_bytes() == destination.read_bytes()
    assert runtime_profile.stat().st_mode & 0o777 == 0o600
    shared = json.loads(hooks.read_text())
    assert "--coordinator-writer" not in shared["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_pilot_profile_registers_only_root_continuity_events(tmp_path: Path) -> None:
    source = tmp_path / "config.toml"
    destination = tmp_path / "coordinator.config.toml"
    runtime_profile = tmp_path / "coordinator.runtime.toml"
    primary = tmp_path / "primary"
    hooks = tmp_path / "hooks.json"
    guard = primary / "scripts/codex-hook"
    continuity = primary / "scripts/codex-continuity-hook"
    primary.mkdir()
    writer = tmp_path / "writer"
    writer.mkdir()
    writer_git_dir = primary / ".git/worktrees/writer"
    writer_git_dir.mkdir(parents=True)
    (writer / ".git").write_text(f"gitdir: {writer_git_dir}\n")
    state = tmp_path / "coordinator"
    source.write_text("")
    inspector = primary / "scripts/codex-worker-inspect"
    for script in (guard, continuity, inspector):
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (primary / "scripts/codex-compact-hook").write_text("#!/bin/sh\nexit 0\n")
    (primary / "scripts/coordinator-control").write_text("#!/bin/sh\nexit 0\n")

    subprocess.run(
        [SCRIPT, source, destination, runtime_profile, primary, writer, hooks,
         guard, continuity, primary / "scripts/codex-compact-hook",
         primary / "scripts/coordinator-control",
         "PILOT", "STANDING", state / "telemetry.jsonl", state / "start-commit",
         state / "launch-manifest.json"],
        check=True,
    )

    rendered = tomllib.loads(destination.read_text())
    assert "mode=PILOT; lifetime=STANDING" in rendered["developer_instructions"]
    assert set(rendered["hooks"]) == {
        "PreToolUse", "SessionStart", "UserPromptSubmit", "Stop"
    }
    assert "SubagentStop" not in rendered["hooks"]
    for event in ("UserPromptSubmit", "Stop"):
        command = rendered["hooks"][event][0]["hooks"][0]["command"]
        assert str(continuity) in command
        assert "--mode PILOT --lifetime STANDING" in command
        assert f"--telemetry {state / 'telemetry.jsonl'}" in command


def test_new_profile_omits_removed_preferences_without_replacing_prior_profile(
    tmp_path: Path,
) -> None:
    source = tmp_path / "config.toml"
    destination = tmp_path / "coordinator.config.toml"
    successor = tmp_path / "successor.config.toml"
    primary = tmp_path / "primary"
    primary.mkdir()
    hooks = tmp_path / "hooks.json"
    source.write_text('model_auto_compact_token_limit = 200000\n')
    prepare(source, destination, primary, hooks)
    original = destination.read_bytes()
    source.write_text('[apps.dish.tools.write]\napproval_mode = "approve"\n')

    with pytest.raises(subprocess.CalledProcessError):
        prepare(source, destination, primary, hooks)

    prepare(source, successor, primary, hooks)

    profile = tomllib.loads(successor.read_text())
    assert "model_auto_compact_token_limit" not in profile
    assert "apps" not in profile
    assert profile["approval_policy"] == "never"
    assert profile["sandbox_mode"] == "danger-full-access"
    assert destination.read_bytes() == original
