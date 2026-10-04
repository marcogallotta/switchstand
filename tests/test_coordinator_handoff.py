from __future__ import annotations

import json
import os
import runpy
import subprocess
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/coordinator-handoff"


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", repo, *arguments], text=True).strip()


def setup(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    home = tmp_path / "home"
    primary = home / "switchstand"
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", remote], check=True,
                   capture_output=True)
    primary.mkdir(parents=True)
    subprocess.run(["git", "-C", primary, "init", "-b", "main"], check=True,
                   capture_output=True)
    for repo in (primary,):
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.invalid")
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
        "session": {"generation": "outgoing-generation", "start_commit": started,
                    "start_record": str(record)},
        "frozen_controls": [], "rereadable_controls": {},
        "unresolved_rereadable_controls": [], "unproved_frozen_controls": [],
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


def test_fast_forwards_clean_main_and_prepares_actual_successor(tmp_path: Path) -> None:
    home, primary, record, started, final = setup(tmp_path)

    result = subprocess.run(
        [SCRIPT, record, obligations(home)], env=os.environ | {"HOME": str(home)},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert git(primary, "rev-parse", "HEAD") == final
    assert git(primary, "status", "--porcelain") == ""
    assert f"{started}..{final}" in result.stdout
    assert "WAITING_FOR_SUCCESSOR" in result.stdout
    artifact = Path(result.stdout.split("Evidence: ", 1)[1].strip())
    assert artifact.parent.stat().st_mode & 0o777 == 0o700
    assert {path.name for path in artifact.iterdir()} == {"obligations", "handoff.json", "result.json"}
    handoff = json.loads((artifact / "handoff.json").read_text())
    assert handoff["state"] == "AWAITING_SUCCESSOR"
    assert handoff["target_commit"] == final
    pending = json.loads((record.parent / "pending-handoff.json").read_text())
    assert pending == {"artifact": str(artifact), "handoff_id": handoff["handoff_id"]}
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in artifact.iterdir())


def test_dirty_main_fails_before_fetch_or_pending_handoff(tmp_path: Path) -> None:
    home, primary, record, _started, _final = setup(tmp_path)
    (primary / "untracked.txt").write_text("preserve me\n")

    result = subprocess.run(
        [SCRIPT, record, obligations(home)], env=os.environ | {"HOME": str(home)},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode != 0
    assert "refuses dirty main" in result.stderr
    assert (primary / "untracked.txt").read_text() == "preserve me\n"
    assert not (record.parent / "pending-handoff.json").exists()
    artifact = Path(result.stderr.strip().rsplit("evidence: ", 1)[1])
    evidence = json.loads((artifact / "result.json").read_text())
    assert evidence["status"] == "FAILED"
    assert evidence["stage"] == "validate-inputs"
    assert evidence["known_head"] == git(primary, "rev-parse", "HEAD")


def test_existing_pending_handoff_fails_before_fetch(tmp_path: Path) -> None:
    home, primary, record, started, _final = setup(tmp_path)
    (record.parent / "pending-handoff.json").write_text("{}")

    result = subprocess.run(
        [SCRIPT, record, obligations(home)], env=os.environ | {"HOME": str(home)},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode != 0
    assert "prior Coordinator handoff is still pending" in result.stderr
    assert git(primary, "rev-parse", "HEAD") == started


def test_git_is_noninteractive_and_timeout_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(SCRIPT), run_name="coordinator_handoff_test")
    git_function = namespace["git"]
    seen: dict[str, Any] = {}

    def timeout_run(*args: Any, **kwargs: Any) -> NoReturn:
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(git_function.__globals__["subprocess"], "run", timeout_run)
    try:
        git_function(Path("/repo"), "fetch", "origin")
    except SystemExit as error:
        assert "git fetch origin timed out after 30s" in str(error)
    else:
        raise AssertionError("timeout must fail the handoff")
    assert seen["timeout"] == 30
    assert cast(dict[str, str], seen["env"])["GIT_TERMINAL_PROMPT"] == "0"
