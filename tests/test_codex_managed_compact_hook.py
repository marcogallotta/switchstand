from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
HOOK = ROOT / "scripts/codex-managed-compact-hook"


def run_hook(event: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [HOOK], input=json.dumps(event), text=True, capture_output=True, check=False
    )


def test_managed_compact_hook_restores_only_current_worker_phase() -> None:
    result = run_hook({"hook_event_name": "SessionStart", "source": "compact"})

    assert result.returncode == 0
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "SessionStart"
    context = output["additionalContext"]
    assert 'work_get(api_version="1") without a WorkId' in context
    assert "exact open review/message/watch obligation" in context
    assert "implementation rereads its current Implementation Specification/Execution Plan" in context
    assert "review rereads its current review procedure, immutable candidate" in context
    assert "missing or stale role/phase binding" in context
    assert "Do not load the Root Coordinator tracking contract" in context
    assert "Markdown dependency graph" in context


def test_managed_compact_hook_scopes_invalid_input_to_role_phase_recovery() -> None:
    result = run_hook([])

    assert result.returncode == 0
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "is UNKNOWN" in context
    assert "Hold only work that depends on recovered role/phase context" in context
