from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/coordinator-control"


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", repo, *arguments], text=True).strip()


def setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", repo, "init", "-b", "main"], check=True, capture_output=True)
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "docs").mkdir()
    (repo / "AGENTS.md").write_text("Read [procedure](docs/procedure.md).\n")
    (repo / "docs/procedure.md").write_text("current\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "start")
    home = tmp_path / "home"
    start = home / ".local/state/switchstand/codex/coordinator/start-commit.test"
    start.parent.mkdir(parents=True)
    start.write_text(git(repo, "rev-parse", "HEAD") + "\n")
    for name in ("profile", "hooks", "executable", "shim"):
        path = tmp_path / name
        path.write_text(name + "\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    result = subprocess.run(
        [
            SCRIPT,
            "create",
            "--repo",
            repo,
            "--control-root",
            ROOT,
            "--start-record",
            start,
            "--profile",
            tmp_path / "profile",
            "--hooks",
            tmp_path / "hooks",
            "--executable",
            tmp_path / "executable",
            "--shim",
            tmp_path / "shim",
            "--invocation-digest",
            "a" * 64,
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return repo, start, Path(result.stdout.strip())


def check(
    manifest: Path, trigger: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    result = subprocess.run(
        [SCRIPT, "check", manifest, "--trigger", trigger],
        text=True,
        capture_output=True,
        check=False,
    )
    return result, json.loads(result.stdout)


def test_launch_manifest_records_identity_and_transitive_reread_set(tmp_path: Path) -> None:
    repo, _start, manifest_path = setup(tmp_path)
    manifest = json.loads(manifest_path.read_text())

    assert manifest["session"]["start_commit"] == git(repo, "rev-parse", "HEAD")
    assert set(manifest["rereadable_controls"]) == {"AGENTS.md", "docs/procedure.md"}

    result, status = check(manifest_path, "post-compaction")
    assert result.returncode == 0
    assert status["state"] == "CURRENT"
    assert status["reread_required"] == ["AGENTS.md", "docs/procedure.md"]
    assert status["required_live_reads"] == ["CURRENT_WORK", "OPEN_OBLIGATIONS", "START_COMMIT"]
    assert status["component_currentness"] == {
        "runtime:effective-client-tool-surface": "CURRENTNESS_UNKNOWN"
    }


def test_frozen_mismatch_is_monotonic_for_generation(tmp_path: Path) -> None:
    _repo, _start, manifest_path = setup(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    profile = Path(
        next(
            item["path"]
            for item in manifest["frozen_controls"]
            if item["id"] == "generated:profile"
        )
    )
    original = profile.read_text()
    profile.write_text("changed\n")

    result, status = check(manifest_path, "post-sync")
    assert result.returncode == 3
    assert status["state"] == "CONTROL_STALE"
    assert status["frozen_mismatches"] == ["generated:profile"]

    profile.write_text(original)
    _result, repeated = check(manifest_path, "post-sync")
    assert repeated == status


def test_post_sync_reports_only_changed_rereadable_dependencies(tmp_path: Path) -> None:
    repo, _start, manifest_path = setup(tmp_path)
    (repo / "docs/procedure.md").write_text("new\n")

    result, status = check(manifest_path, "post-sync")

    assert result.returncode == 0
    assert status["state"] == "CURRENT"
    assert status["reread_required"] == ["docs/procedure.md"]


def test_missing_frozen_component_is_unknown_not_stale(tmp_path: Path) -> None:
    _repo, _start, manifest_path = setup(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    profile = Path(
        next(
            item["path"]
            for item in manifest["frozen_controls"]
            if item["id"] == "generated:profile"
        )
    )
    profile.unlink()

    result, status = check(manifest_path, "post-sync")

    assert result.returncode == 2
    assert status["state"] == "CURRENTNESS_UNKNOWN"
    assert status["unknown_components"] == ["generated:profile"]
