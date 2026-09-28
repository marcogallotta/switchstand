from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "codex-coordinator-profile"


def test_profile_copies_only_benign_user_preferences(tmp_path: Path) -> None:
    source = tmp_path / "config.toml"
    destination = tmp_path / "coordinator.config.toml"
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

    subprocess.run([SCRIPT, source, destination], check=True)

    assert tomllib.loads(destination.read_text()) == {
        "model_auto_compact_token_limit": 200000,
        "model_auto_compact_token_limit_scope": "total",
        "tui": {"alternate_screen": "never"},
        "notice": {"hide_rate_limit_model_nudge": True},
    }
    assert destination.stat().st_mode & 0o777 == 0o600


def test_profile_replaces_removed_preferences(tmp_path: Path) -> None:
    source = tmp_path / "config.toml"
    destination = tmp_path / "coordinator.config.toml"
    source.write_text('model_auto_compact_token_limit = 200000\n')
    subprocess.run([SCRIPT, source, destination], check=True)
    source.write_text('[apps.dish.tools.write]\napproval_mode = "approve"\n')

    subprocess.run([SCRIPT, source, destination], check=True)

    assert destination.read_text() == ""
