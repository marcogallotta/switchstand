from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from switchstand.coordinator_workers import CoordinatorWorkers


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", repo, *arguments], text=True).strip()


def test_spawn_persists_prepared_identity_before_start(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    repo = home / "switchstand"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", repo, "init", "-b", "main"], check=True, capture_output=True)
    git(repo, "config", "user.name", "Worker Test")
    git(repo, "config", "user.email", "worker@example.invalid")
    (repo / "tracked").write_text("base\n")
    git(repo, "add", "tracked")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    control = home / ".local/state/switchstand/control"
    control.mkdir(parents=True)
    (control / "manifest").write_text(f"state=ACTIVE\ncontrol_sha={base}\n")
    subject = CoordinatorWorkers(home, state_root=tmp_path / "state")
    monkeypatch.setattr(subject, "_command", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    operation = uuid4()
    work_id = uuid4()

    result = subject.spawn(operation, work_id, "objective")

    record = subject.store.load(operation)
    assert result.status == "started"
    assert record is not None and record.phase == "PREPARED"
    assert record.work_id == work_id and record.base_sha == base
    assert record.command[:2] == (str(repo / "scripts/switchstand"), "--isolated")
