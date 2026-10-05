from __future__ import annotations

import json
import os
import runpy
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from _pytest.capture import CaptureFixture
from _pytest.monkeypatch import MonkeyPatch

from switchstand.coordinator_sync import CoordinatorSync

SCRIPT = Path(__file__).parents[1] / "scripts/coordinator-handoff"


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", repo, *arguments], text=True).strip()


def setup(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    home = tmp_path / "home"
    primary = home / "switchstand"
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", remote],
        check=True, capture_output=True,
    )
    primary.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", primary, "init", "-b", "main"], check=True, capture_output=True
    )
    git(primary, "config", "user.name", "Test")
    git(primary, "config", "user.email", "test@example.invalid")
    (primary / "tracked.txt").write_text("start\n")
    git(primary, "add", "tracked.txt")
    git(primary, "commit", "-m", "start")
    started = git(primary, "rev-parse", "HEAD")
    git(primary, "remote", "add", "origin", str(remote))
    git(primary, "push", "-u", "origin", "main")
    subprocess.run(["git", "clone", remote, source], check=True, capture_output=True)
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "tracked.txt").write_text("final\n")
    git(source, "add", "tracked.txt")
    git(source, "commit", "-m", "final")
    final = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "main")
    record = home / ".local/state/switchstand/codex/coordinator/start-commit.test"
    record.parent.mkdir(parents=True)
    record.write_text(started + "\n")
    record.chmod(0o600)
    manifest = {
        "schema_version": 1,
        "session": {
            "generation": "outgoing-generation",
            "start_commit": started,
            "start_record": str(record),
        },
        "frozen_controls": [],
        "rereadable_controls": {},
        "unresolved_rereadable_controls": [],
        "unproved_frozen_controls": [],
    }
    unsigned = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    import hashlib
    manifest["manifest_digest"] = hashlib.sha256(unsigned).hexdigest()
    Path(f"{record}.manifest.json").write_text(json.dumps(manifest))
    return home, primary, record, started, final


def obligations(home: Path) -> Path:
    path = home / ".local/state/switchstand/open-obligations.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("exact open obligations\n")
    return path


def script_main() -> Any:
    return runpy.run_path(str(SCRIPT), run_name="coordinator_handoff_test")["main"]


def synchronizer(home: Path) -> Any:
    remote = home.parent / "remote.git"
    subject = CoordinatorSync(
        home,
        remote_source=str(remote),
        accepted_origins=frozenset({str(remote)}),
        remote_protocol="file",
    )

    def run(observed_home: Path, started: str) -> dict[str, object]:
        assert observed_home == home
        return cast(dict[str, object], subject.handoff(started).model_dump())

    return run


def test_fast_forwards_clean_main_and_prepares_actual_successor(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str],
) -> None:
    home, primary, record, started, final = setup(tmp_path)
    monkeypatch.setenv("HOME", str(home))

    script_main()(
        [str(record), str(obligations(home))], synchronize=synchronizer(home)
    )

    output = capsys.readouterr().out
    assert git(primary, "rev-parse", "HEAD") == final
    assert git(primary, "status", "--porcelain") == ""
    assert f"{started}..{final}" in output
    assert "WAITING_FOR_SUCCESSOR" in output
    artifact = Path(output.split("Evidence: ", 1)[1].strip())
    assert artifact.parent.stat().st_mode & 0o777 == 0o700
    assert {path.name for path in artifact.iterdir()} == {
        "obligations", "handoff.json", "result.json",
    }
    handoff = json.loads((artifact / "handoff.json").read_text())
    assert handoff["state"] == "AWAITING_SUCCESSOR"
    assert handoff["target_commit"] == final
    pending = json.loads((record.parent / "pending-handoff.json").read_text())
    assert pending == {"artifact": str(artifact), "handoff_id": handoff["handoff_id"]}
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in artifact.iterdir())


def test_dirty_main_fails_before_fetch_or_pending_handoff(
    tmp_path: Path, monkeypatch: MonkeyPatch,
) -> None:
    home, primary, record, _started, _final = setup(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    (primary / "untracked.txt").write_text("preserve me\n")

    with pytest.raises(SystemExit, match="refuses dirty main") as raised:
        script_main()(
            [str(record), str(obligations(home))], synchronize=synchronizer(home)
        )

    assert (primary / "untracked.txt").read_text() == "preserve me\n"
    assert not (record.parent / "pending-handoff.json").exists()
    artifact = Path(str(raised.value).rsplit("evidence: ", 1)[1])
    evidence = json.loads((artifact / "result.json").read_text())
    assert evidence["status"] == "FAILED"
    assert evidence["stage"] == "sync-main"
    assert evidence["known_head"] == git(primary, "rev-parse", "HEAD")


def test_existing_pending_handoff_fails_before_sync(
    tmp_path: Path, monkeypatch: MonkeyPatch,
) -> None:
    home, primary, record, started, _final = setup(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    (record.parent / "pending-handoff.json").write_text("{}")

    with pytest.raises(SystemExit, match="prior Coordinator handoff is still pending"):
        script_main()(
            [str(record), str(obligations(home))], synchronize=synchronizer(home)
        )

    assert git(primary, "rev-parse", "HEAD") == started


def test_sync_control_timeout_is_reported(
    monkeypatch: MonkeyPatch, tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(SCRIPT), run_name="coordinator_handoff_test")
    synchronize_main = namespace["synchronize_main"]

    def timeout_run(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(synchronize_main.__globals__["subprocess"], "run", timeout_run)
    with pytest.raises(
        SystemExit, match="Coordinator sync control unavailable: TimeoutExpired"
    ):
        synchronize_main(tmp_path, "a" * 40)
