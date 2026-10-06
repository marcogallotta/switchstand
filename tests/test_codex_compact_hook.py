from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
HOOK = ROOT / "scripts/codex-compact-hook"


def install_hook(tmp_path: Path, checker: str) -> tuple[Path, Path]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    hook = scripts / HOOK.name
    shutil.copy2(HOOK, hook)
    control = scripts / "coordinator-control"
    control.write_text(checker)
    control.chmod(control.stat().st_mode | stat.S_IXUSR)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n")
    return hook, manifest


def run_hook(hook: Path, manifest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [hook, manifest],
        input=json.dumps({"hook_event_name": "SessionStart", "source": "compact"}),
        text=True,
        capture_output=True,
        check=False,
    )


def test_compact_hook_runs_checker_and_injects_bounded_context(tmp_path: Path) -> None:
    hook, manifest = install_hook(
        tmp_path,
        """#!/bin/sh
test "$1" = check
test "$2" = "$EXPECTED_MANIFEST"
test "$3" = --trigger
test "$4" = post-compaction
printf '%s\\n' '{"state":"CURRENTNESS_UNKNOWN","reread_required":["AGENTS.md"],"required_live_reads":{"START_COMMIT":"/state/start-commit.x"},"start_record":"/state/start-commit.x","changed_launch_controls":[],"unknown_components":[],"component_currentness":{"rereadable:AGENTS.md->missing.md":"CURRENTNESS_UNKNOWN"}}'
exit 2
""",
    )
    result = subprocess.run(
        [hook, manifest],
        input=json.dumps({"hook_event_name": "SessionStart", "source": "compact"}),
        text=True,
        capture_output=True,
        env={**os.environ, "EXPECTED_MANIFEST": str(manifest)},
        check=False,
    )

    assert result.returncode == 0
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "SessionStart"
    context = output["additionalContext"]
    assert "reread repository controls: AGENTS.md" in context
    assert "exact start record: /state/start-commit.x" in context
    assert "live state: START_COMMIT=/state/start-commit.x" in context
    assert "Current work remains the preserved active assignment/WorkId" in context
    assert "affected currentness boundary: rereadable:AGENTS.md->missing.md" in context
    assert f"rerun {hook.with_name('coordinator-control')} check {manifest}" in context
    assert "never substitute writer-relative scripts/coordinator-control" in context


def test_compact_hook_injects_currentness_unknown_when_checker_fails(tmp_path: Path) -> None:
    hook, manifest = install_hook(tmp_path, "#!/bin/sh\necho broken >&2\nexit 1\n")

    result = run_hook(hook, manifest)

    assert result.returncode == 0
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "CURRENTNESS_UNKNOWN" in context
    assert "mechanical check failed: broken" in context
    assert (
        f"rerun {hook.with_name('coordinator-control')} check {manifest} "
        "--trigger post-compaction"
    ) in context
    assert "never substitute writer-relative scripts/coordinator-control" in context


def test_compact_hook_injects_currentness_unknown_for_non_object_event(tmp_path: Path) -> None:
    hook, manifest = install_hook(tmp_path, "#!/bin/sh\nexit 99\n")

    result = subprocess.run(
        [hook, manifest], input="[]", text=True, capture_output=True, check=False
    )

    assert result.returncode == 0
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "CURRENTNESS_UNKNOWN" in context
    assert "SessionStart hook input must be a JSON object" in context


def test_compact_hook_injects_currentness_unknown_for_non_object_checker_output(
    tmp_path: Path,
) -> None:
    hook, manifest = install_hook(tmp_path, "#!/bin/sh\necho '[]'\nexit 0\n")

    result = run_hook(hook, manifest)

    assert result.returncode == 0
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "CURRENTNESS_UNKNOWN" in context
    assert "coordinator-control output must be a JSON object" in context


def test_compact_hook_rejects_symbolic_live_reads(tmp_path: Path) -> None:
    hook, manifest = install_hook(
        tmp_path,
        "#!/bin/sh\necho '{\"state\":\"CURRENT\",\"required_live_reads\":[\"START_COMMIT\"]}'\n",
    )

    result = run_hook(hook, manifest)

    assert result.returncode == 0
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "CURRENTNESS_UNKNOWN" in context
    assert "live reads must map names to absolute paths" in context


def test_compact_hook_preserves_checker_owned_unknown_reason(tmp_path: Path) -> None:
    hook, manifest = install_hook(
        tmp_path,
        "#!/bin/sh\necho '{\"state\":\"CURRENTNESS_UNKNOWN\",\"reason\":\"manifest identity is invalid\"}'\nexit 2\n",
    )

    result = run_hook(hook, manifest)

    assert result.returncode == 0
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Reason: manifest identity is invalid" in context
    assert "affected currentness boundary: launch-control-currentness" in context
    assert (
        f"rerun {hook.with_name('coordinator-control')} check {manifest} "
        "--trigger post-compaction"
    ) in context
    assert "never substitute writer-relative scripts/coordinator-control" in context
