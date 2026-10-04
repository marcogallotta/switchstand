from __future__ import annotations

import json
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
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    continuity_hook.write_text("#!/bin/sh\nexit 0\n")
    continuity_hook.chmod(continuity_hook.stat().st_mode | stat.S_IXUSR)
    runtime_profile = destination.with_name(f"{destination.stem}.runtime.toml")
    writer = primary.parent / "writer"
    writer.mkdir(exist_ok=True)
    subprocess.run(
        [SCRIPT, source, destination, runtime_profile, primary, writer, hooks, hook, continuity_hook,
         "OFF", "ASSIGNMENT", primary / "telemetry.jsonl", primary / "start-commit",
         primary / "launch-manifest.json"],
        check=True,
    )
    return runtime_profile


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
    assert profile["default_permissions"] == "switchstand-coordinator"
    instructions = profile["developer_instructions"]
    assert f"Coordinator start commit is recorded at {primary / 'start-commit'}." in instructions
    assert str(primary / "launch-manifest.json") in instructions
    assert "--trigger post-compaction" in instructions
    assert f"owned linked writer is {primary.parent / 'writer'}" in instructions
    assert "Changed launch controls require only the reported affected-boundary recheck" in instructions
    assert "mode=OFF; lifetime=ASSIGNMENT" in instructions
    assert profile["features"] == {"hooks": True}
    assert profile["mcp_servers"] == {
        "switchstand_coordinator_control": {
            "command": str(primary / "scripts/switchstand-coordinator-control-mcp"),
            "required": True,
            "default_tools_approval_mode": "approve",
            "enabled_tools": ["coordinator_currentness_get", "coordinator_main_sync"],
        }
    }
    filesystem = profile["permissions"]["switchstand-coordinator"]["filesystem"]
    assert filesystem == {
        ":root": "read",
        str(primary.parent): "write",
        ":tmpdir": "write",
        ":slash_tmp": "write",
        str(primary.parent / "writer"): "write",
        str(primary): {".": "read", ".git": "write"},
    }
    assert profile["permissions"]["switchstand-coordinator"]["network"] == {
        "enabled": True,
        "dangerously_allow_all_unix_sockets": True,
    }
    assert profile["model_auto_compact_token_limit"] == 200000
    assert profile["model_auto_compact_token_limit_scope"] == "total"
    assert profile["tui"] == {"alternate_screen": "never"}
    assert profile["notice"] == {"hide_rate_limit_model_nudge": True}
    assert set(profile) == {
        "approval_policy", "default_permissions", "developer_instructions", "features", "permissions",
        "mcp_servers",
        "model_auto_compact_token_limit", "model_auto_compact_token_limit_scope",
        "tui", "notice",
    }
    hook_config = json.loads(hooks.read_text())
    assert hook_config["description"] == "Switchstand Coordinator shared-primary guard."
    group = hook_config["hooks"]["PreToolUse"][0]
    assert group["matcher"] == "^(Bash|apply_patch)$"
    assert group["hooks"][0]["command"] == (
        f"{primary / 'scripts/codex-hook'} --coordinator-primary {primary} "
        f"--coordinator-writer {primary.parent / 'writer'}"
    )
    assert set(hook_config["hooks"]) == {"PreToolUse"}
    assert destination.stat().st_mode & 0o777 == 0o600
    assert hooks.stat().st_mode & 0o777 == 0o600
    assert runtime_profile.read_bytes() == destination.read_bytes()
    assert runtime_profile.stat().st_mode & 0o777 == 0o600


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
    source.write_text("")
    for script in (guard, continuity):
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

    subprocess.run(
        [SCRIPT, source, destination, runtime_profile, primary, writer, hooks, guard, continuity,
         "PILOT", "STANDING", primary / "telemetry.jsonl", primary / "start-commit",
         primary / "launch-manifest.json"],
        check=True,
    )

    rendered = tomllib.loads(destination.read_text())
    assert "mode=PILOT; lifetime=STANDING" in rendered["developer_instructions"]
    assert set(rendered["hooks"]) == {"UserPromptSubmit", "Stop"}
    assert "SubagentStop" not in rendered["hooks"]
    for event in ("UserPromptSubmit", "Stop"):
        command = rendered["hooks"][event][0]["hooks"][0]["command"]
        assert str(continuity) in command
        assert "--mode PILOT --lifetime STANDING" in command
        assert f"--telemetry {primary / 'telemetry.jsonl'}" in command
    shared = json.loads(hooks.read_text())
    assert set(shared["hooks"]) == {"PreToolUse"}


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
    assert destination.read_bytes() == original
