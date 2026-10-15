from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/switchstand-backup"
SPEC = importlib.util.spec_from_loader(
    "switchstand_backup", SourceFileLoader("switchstand_backup", str(SCRIPT))
)
assert SPEC is not None and SPEC.loader is not None
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


def test_tier_schedule_and_retention(tmp_path: Path) -> None:
    now = datetime(2026, 10, 12, 0, tzinfo=UTC)
    assert backup.selected_tiers(now) == ["hourly", "12-hourly", "daily", "weekly"]
    old = tmp_path / "hourly/old.dump"
    old.parent.mkdir()
    old.write_bytes(b"old")
    old.with_suffix(".json").write_text("{}")
    timestamp = (now - timedelta(hours=25)).timestamp()
    os.utime(old, (timestamp, timestamp))

    backup.prune(tmp_path, now)

    assert not old.exists()
    assert not old.with_suffix(".json").exists()


def test_metadata_records_required_pairing(tmp_path: Path) -> None:
    dump = tmp_path / "subject.dump"
    dump.write_bytes(b"dump")
    fingerprints = {"WorkIds": "a", "messages": "b", "grants": "c"}
    result = backup.metadata(
        datetime(2026, 10, 15, tzinfo=UTC),
        dump,
        fingerprints,
        "0025_task_control_checkpoints",
        "a" * 40,
    )

    assert result["deployed_commit"] == "a" * 40
    assert result["db_migration_version"] == "0025_task_control_checkpoints"
    assert result["created_at"] == "2026-10-15T00:00:00+00:00"
    assert result["dump_sha256"]
    assert result["data_fingerprints"] == fingerprints


def test_latest_backup_rejects_an_incomplete_or_tampered_pair(tmp_path: Path) -> None:
    hourly = tmp_path / "hourly"
    hourly.mkdir()
    dump = hourly / "subject.dump"
    dump.write_bytes(b"dump")
    with pytest.raises(RuntimeError, match="no complete validated"):
        backup.latest_backup(tmp_path)
    dump.with_suffix(".json").write_text(
        json.dumps(
            {
                "created_at": "2026-10-15T00:00:00+00:00",
                "data_fingerprints": {"WorkIds": "a", "messages": "b", "grants": "c"},
                "db_migration_version": "0025_task_control_checkpoints",
                "deployed_commit": "a" * 40,
                "dump_sha256": backup.hash_file(dump),
                "dump_size": dump.stat().st_size,
            }
        )
    )
    assert backup.latest_backup(tmp_path) == dump
    dump.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="no complete validated"):
        backup.latest_backup(tmp_path)


def test_stale_check_fires_visibly(tmp_path: Path) -> None:
    hourly = tmp_path / "hourly"
    hourly.mkdir()
    subject = hourly / "old.dump"
    subject.write_bytes(b"dump")
    subject.with_suffix(".json").write_text(
        json.dumps(
            {
                "created_at": "2026-10-15T09:00:00+00:00",
                "data_fingerprints": {"WorkIds": "a", "messages": "b", "grants": "c"},
                "db_migration_version": "0025_task_control_checkpoints",
                "deployed_commit": "a" * 40,
                "dump_sha256": backup.hash_file(subject),
                "dump_size": subject.stat().st_size,
            }
        )
    )
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    old = (now - timedelta(hours=3)).timestamp()
    os.utime(subject, (old, old))

    result = subprocess.run(
        [str(SCRIPT), "check-stale", "--backup-root", str(tmp_path), "--max-age-hours", "2"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "switchstand backup alert: backup stale" in result.stderr


def test_installer_enables_only_backup_timers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    source = repo / "scripts/switchstand-backup"
    source.parent.mkdir(parents=True)
    source.write_text("#!/bin/sh\nexit 0\n")
    source.chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    systemctl = tools / "systemctl"
    systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/systemctl-calls"\n')
    systemctl.chmod(0o755)

    subprocess.run(
        [str(Path(__file__).parents[1] / "scripts/install-switchstand-backups"), str(repo)],
        check=True,
        env=os.environ | {"HOME": str(home), "PATH": f"{tools}:{os.environ['PATH']}"},
    )

    units = home / ".config/systemd/user"
    timer = (units / "switchstand-backup.timer").read_text()
    assert "OnActiveSec=10s" in timer and "OnCalendar=hourly" in timer
    assert "OnCalendar=*:0/15" in (units / "switchstand-backup-stale.timer").read_text()
    assert "--max-age-hours=2" in (units / "switchstand-backup-stale.service").read_text()
    calls = (home / "systemctl-calls").read_text()
    assert "enable --now switchstand-backup.timer switchstand-backup-stale.timer" in calls
    assert "restart switchstand-backup.timer switchstand-backup-stale.timer" in calls
