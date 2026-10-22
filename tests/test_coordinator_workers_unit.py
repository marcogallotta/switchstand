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
    (control / "manifest").write_text(f"state=ACTIVE\ncontrol_sha={base}\ncontrol_path={repo}\n")
    subject = CoordinatorWorkers(home, state_root=tmp_path / "state")
    command: tuple[str, ...] = ()
    launches = 0
    unit_state = "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"

    def capture(*args, **_kwargs):
        nonlocal command, launches
        if args[0] == "systemd-run":
            command = args
            launches += 1
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(
            returncode=0,
            stdout=unit_state,
            stderr="",
        )

    monkeypatch.setattr(subject, "_command", capture)
    operation = uuid4()
    work_id = uuid4()

    result = subject.spawn(operation, work_id, "objective")

    record = subject.store.load(operation)
    assert result.status == "started"
    assert record is not None and record.phase == "PREPARED"
    assert record.work_id == work_id and record.base_sha == base
    assert record.git_common == str((repo / ".git").resolve())
    assert record.command[:2] == (str(repo / "scripts/switchstand"), "--isolated")
    assert "--collect" in command
    replay = subject.spawn(operation, work_id, "objective")
    assert replay.status == "unknown"
    assert replay.reason == "unit_start_not_applied"
    assert replay.spawn_id == operation and replay.work_id == work_id
    assert launches == 1
    assert subject.spawn(uuid4(), work_id, "objective").status == "unknown"
    assert launches == 1

    unit_state = "LoadState=loaded\nActiveState=activating\nSubState=start\n"
    pending = subject.spawn(operation, work_id, "objective")
    assert pending.status == "unknown" and pending.reason == "worker_start_pending"
    assert launches == 1

    for active, sub in (("failed", "failed"), ("inactive", "dead")):
        unit_state = f"LoadState=loaded\nActiveState={active}\nSubState={sub}\n"
        lost_prepared = subject.spawn(operation, work_id, "objective")
        assert lost_prepared.status == "unknown" and lost_prepared.reason == "execution_lost"
        assert lost_prepared.spawn_id == operation and lost_prepared.work_id == work_id
        assert subject.status(operation).reason == "execution_lost"
        assert launches == 1

    unit_state = "LoadState=loaded\nActiveState=active\nSubState=running\n"
    active_prepared = subject.spawn(operation, work_id, "objective")
    assert active_prepared.status == "unknown" and active_prepared.reason == "worker_start_pending"
    assert launches == 1

    subject.store.write(record.model_copy(update={"phase": "RUNNING"}), replace=True)
    restarted = CoordinatorWorkers(home, state_root=tmp_path / "state")
    monkeypatch.setattr(restarted, "_command", capture)
    replayed = restarted.spawn(operation, work_id, "objective")
    assert replayed.status == "replayed" and replayed.reason == "operation_replayed_running"
    assert replayed.spawn_id == operation and replayed.work_id == work_id
    assert restarted.spawn(operation, work_id, "different").reason == "operation_identity_conflict"
    assert launches == 1

    unit_state = "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
    lost = restarted.spawn(operation, work_id, "objective")
    assert lost.status == "unknown" and lost.reason == "execution_lost"
    assert launches == 1

    subject.store.write(record.model_copy(update={"phase": "TERMINAL", "exit_status": 1}), replace=True)
    terminal = restarted.spawn(operation, work_id, "objective")
    assert terminal.status == "replayed" and terminal.reason == "operation_replayed_terminal"
    assert launches == 1
