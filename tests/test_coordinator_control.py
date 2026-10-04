from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/coordinator-control"


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", repo, *arguments], text=True).strip()


def setup(
    tmp_path: Path, *, successor_eligible: bool = False, advance_primary: bool = False
) -> tuple[Path, Path, Path]:
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
    writer = tmp_path / "writer"
    subprocess.run(
        ["git", "-C", repo, "worktree", "add", "-b", "writer", writer, "HEAD"],
        check=True, capture_output=True,
    )
    home = tmp_path / "home"
    start = home / ".local/state/switchstand/codex/coordinator/start-commit.test"
    start.parent.mkdir(parents=True)
    start.write_text(git(repo, "rev-parse", "HEAD") + "\n")
    if advance_primary:
        (repo / "later.txt").write_text("later canonical main\n")
        git(repo, "add", "later.txt")
        git(repo, "commit", "-m", "advance canonical main")
    for name in ("profile", "runtime-profile", "hooks", "executable", "shim"):
        path = tmp_path / name
        path.write_text(("profile" if name == "runtime-profile" else name) + "\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    command = [
            SCRIPT,
            "create",
            "--repo",
            repo,
            "--writer",
            writer,
            "--control-root",
            ROOT,
            "--start-record",
            start,
            "--profile",
            tmp_path / "profile",
            "--runtime-profile",
            tmp_path / "runtime-profile",
            "--hooks",
            tmp_path / "hooks",
            "--executable",
            tmp_path / "executable",
            "--shim",
            tmp_path / "shim",
            "--invocation-digest",
            "a" * 64,
        ]
    if successor_eligible:
        command.append("--successor-eligible")
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=True,
    )
    return repo, start, Path(result.stdout.strip())


def pending_handoff(start: Path, commit: str) -> Path:
    artifact = start.parent.parent / "handoffs/handoff-test"
    artifact.mkdir(parents=True)
    (artifact / "obligations").write_text("continue exact work\n")
    obligations_hash = hashlib.sha256((artifact / "obligations").read_bytes()).hexdigest()
    handoff = {"handoff_id": "handoff-id", "state": "AWAITING_SUCCESSOR",
               "target_commit": commit, "obligations_sha256": obligations_hash}
    (artifact / "handoff.json").write_text(json.dumps(handoff))
    pointer = {"handoff_id": "handoff-id", "artifact": str(artifact)}
    (start.parent / "pending-handoff.json").write_text(json.dumps(pointer))
    return artifact


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


def test_manifest_creation_keeps_recorded_writer_when_canonical_main_advances(
    tmp_path: Path,
) -> None:
    repo, start, manifest_path = setup(tmp_path, advance_primary=True)
    manifest = json.loads(manifest_path.read_text())

    assert manifest["session"]["start_commit"] == start.read_text().strip()
    assert manifest["session"]["start_commit"] != git(repo, "rev-parse", "HEAD")
    assert git(Path(manifest["session"]["writer"]), "rev-parse", "HEAD") == (
        manifest["session"]["start_commit"]
    )
    result, status = check(manifest_path, "post-sync")
    assert result.returncode == 0
    assert status["state"] == "CURRENT"
    assert status["current_commit"] == git(repo, "rev-parse", "HEAD")


def test_changed_launch_control_requires_bounded_recheck_without_staling_generation(
    tmp_path: Path,
) -> None:
    _repo, _start, manifest_path = setup(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    profile = Path(
        next(
            item["path"]
            for item in manifest["launch_controls"]
            if item["id"] == "generated:profile-snapshot"
        )
    )
    original = profile.read_text()
    profile.write_text("changed\n")

    result, status = check(manifest_path, "post-sync")
    assert result.returncode == 0
    assert status["state"] == "CURRENT"
    assert status["changed_launch_controls"] == ["generated:profile-snapshot"]
    assert status["recheck_required"] == ["generated:profile-snapshot"]
    assert status["component_currentness"]["generated:profile-snapshot"] == (
        "CHANGED_RECHECK_REQUIRED"
    )

    profile.write_text(original)
    _result, repeated = check(manifest_path, "post-sync")
    assert repeated["state"] == "CURRENT"
    assert repeated["changed_launch_controls"] == []
    assert repeated["recheck_required"] == []


def test_runtime_model_persistence_does_not_stale_launch_identity(tmp_path: Path) -> None:
    _repo, _start, manifest_path = setup(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    runtime_profile = Path(manifest["runtime_mutable_controls"][0]["path"])
    runtime_profile.write_text(
        runtime_profile.read_text()
        + 'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "medium"\n'
    )

    result, status = check(manifest_path, "post-sync")

    assert result.returncode == 0
    assert status["state"] == "CURRENT"
    assert "frozen_mismatches" not in status


def test_post_sync_reports_only_changed_rereadable_dependencies(tmp_path: Path) -> None:
    repo, _start, manifest_path = setup(tmp_path)
    (repo / "docs/procedure.md").write_text("new\n")

    result, status = check(manifest_path, "post-sync")

    assert result.returncode == 0
    assert status["state"] == "CURRENT"
    assert status["reread_required"] == ["docs/procedure.md"]


def test_missing_launch_component_is_unknown(tmp_path: Path) -> None:
    _repo, _start, manifest_path = setup(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    profile = Path(
        next(
            item["path"]
            for item in manifest["launch_controls"]
            if item["id"] == "generated:profile-snapshot"
        )
    )
    profile.unlink()

    result, status = check(manifest_path, "post-sync")

    assert result.returncode == 2
    assert status["state"] == "CURRENTNESS_UNKNOWN"
    assert status["unknown_components"] == ["generated:profile-snapshot"]


def test_corrupt_launch_profile_receipt_is_currentness_unknown(tmp_path: Path) -> None:
    _repo, _start, manifest_path = setup(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["runtime_mutable_controls"][0]["launch_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))

    result, status = check(manifest_path, "post-sync")

    assert result.returncode == 2
    assert status["state"] == "CURRENTNESS_UNKNOWN"
    assert "manifest identity is invalid" in status["reason"]


def test_legacy_v1_mismatch_becomes_bounded_recheck_during_upgrade(tmp_path: Path) -> None:
    _repo, _start, manifest_path = setup(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 1
    manifest.pop("runtime_mutable_controls")
    manifest["frozen_controls"] = manifest.pop("launch_controls")
    manifest["unproved_frozen_controls"] = manifest.pop("unproved_launch_controls")
    control = next(
        item
        for item in manifest["frozen_controls"]
        if item["id"] == "repository:scripts/coordinator-control"
    )
    control["sha256"] = "0" * 64
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    manifest["manifest_digest"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest))

    result, status = check(manifest_path, "post-sync")

    assert result.returncode == 0
    assert status["state"] == "CURRENT"
    assert status["changed_launch_controls"] == ["repository:scripts/coordinator-control"]
    assert status["recheck_required"] == ["repository:scripts/coordinator-control"]


def test_unresolved_document_dependencies_are_explicit_component_unknowns(
    tmp_path: Path,
) -> None:
    repo, _start, manifest_path = setup(tmp_path)
    (repo / "folder.md").mkdir()
    (repo / "bad.md").write_bytes(b"\xff")
    (tmp_path / "outside.md").write_text("outside\n")
    (repo / "AGENTS.md").write_text(
        "[missing](missing.md) [outside](../outside.md) [directory](folder.md) "
        "[unreadable](bad.md) [unsupported](file:control.md)\n"
    )

    result, status = check(manifest_path, "post-sync")

    assert result.returncode == 2
    assert status["state"] == "CURRENTNESS_UNKNOWN"
    assert {
        (item["target"], item["reason"]) for item in status["unresolved_rereadable_controls"]
    } == {
        ("missing.md", "missing"),
        ("../outside.md", "outside_root"),
        ("folder.md", "unsupported_file_type"),
        ("bad.md", "unreadable"),
        ("file:control.md", "unsupported_scheme"),
    }
    assert all(
        status["component_currentness"][f"rereadable:{item['source']}->{item['target']}"]
        == "CURRENTNESS_UNKNOWN"
        for item in status["unresolved_rereadable_controls"]
    )


def test_actual_successor_launch_is_bound_and_must_acknowledge(tmp_path: Path) -> None:
    repo, start, first = setup(tmp_path)
    first.unlink()
    artifact = pending_handoff(start, git(repo, "rev-parse", "HEAD"))
    profile = tmp_path / "profile"
    command = [SCRIPT, "create", "--repo", repo, "--writer", tmp_path / "writer",
               "--control-root", ROOT,
               "--start-record", start, "--profile", profile,
               "--runtime-profile", tmp_path / "runtime-profile",
               "--hooks", tmp_path / "hooks", "--executable", tmp_path / "executable",
               "--shim", tmp_path / "shim", "--invocation-digest", "b" * 64, "--successor-eligible"]
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    manifest_path = Path(result.stdout.strip())
    manifest = json.loads(manifest_path.read_text())
    assert manifest["handoff"]["handoff_id"] == "handoff-id"
    proof = json.loads((artifact / "successor-launch.json").read_text())
    assert proof["session_generation"] == manifest["session"]["generation"]
    assert json.loads((artifact / "handoff.json").read_text())["state"] == "SUCCESSOR_LAUNCHED"
    assert (start.parent / "pending-handoff.json").exists()

    original = (artifact / "obligations").read_text()
    (artifact / "obligations").write_text("tampered\n")
    rejected = subprocess.run([SCRIPT, "acknowledge", manifest_path], capture_output=True, check=False)
    assert rejected.returncode != 0 and (start.parent / "pending-handoff.json").exists()
    (artifact / "obligations").write_text(original)
    acknowledged = subprocess.run([SCRIPT, "acknowledge", manifest_path], text=True,
                                  capture_output=True, check=True)
    assert json.loads(acknowledged.stdout)["state"] == "ACKNOWLEDGED"
    assert json.loads((artifact / "handoff.json").read_text())["state"] == "ACKNOWLEDGED"
    assert json.loads((artifact / "successor-ack.json").read_text())["acknowledgement"] == \
        "SECONDARY_COORDINATOR_ACK"
    assert not (start.parent / "pending-handoff.json").exists()


def test_ineligible_launch_does_not_consume_pending_handoff(tmp_path: Path) -> None:
    repo, start, first = setup(tmp_path)
    first.unlink()
    artifact = pending_handoff(start, git(repo, "rev-parse", "HEAD"))
    result = subprocess.run(
        [SCRIPT, "create", "--repo", repo, "--writer", tmp_path / "writer",
         "--control-root", ROOT,
         "--start-record", start, "--profile", tmp_path / "profile",
         "--runtime-profile", tmp_path / "runtime-profile",
         "--hooks", tmp_path / "hooks", "--executable", tmp_path / "executable",
         "--shim", tmp_path / "shim", "--invocation-digest", "c" * 64],
        text=True, capture_output=True, check=True,
    )
    manifest_path = Path(result.stdout.strip())
    manifest = json.loads(manifest_path.read_text())
    assert "handoff" not in manifest
    assert not (artifact / "successor-launch.json").exists()
    assert (start.parent / "pending-handoff.json").exists()
