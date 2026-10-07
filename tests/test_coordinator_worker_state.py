from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from switchstand.coordinator_worker_state import WorkerRecord, WorkerStore

WORK = UUID("11111111-1111-4111-8111-111111111111")


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", repo, *arguments], text=True).strip()


def store(tmp_path: Path) -> tuple[WorkerStore, str]:
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
    (control / "manifest").write_text(
        f"state=ACTIVE\ncontrol_sha={base}\ncontrol_path={repo}\n"
    )
    return WorkerStore(home, tmp_path / "state"), base


def test_records_are_durable_and_corruption_fails_closed(tmp_path: Path) -> None:
    subject, base = store(tmp_path)
    spawn_id = uuid4()
    record = WorkerRecord(
        version=1,
        spawn_id=spawn_id,
        work_id=WORK,
        objective="objective",
        writer="/missing",
        branch=f"v2-task-{WORK}",
        base_sha=base,
        unit="unit.service",
        log="/log",
        started_at=1,
        command=("true",),
    )
    with subject.locked():
        subject.write(record)
    assert subject.load(spawn_id) == record

    damaged = subject.root / f"{uuid4()}.json"
    damaged.write_text("not json\n")
    damaged.chmod(0o600)
    with pytest.raises(ValueError, match="state_corrupt"):
        list(subject.records())


def test_candidate_requires_exact_writer_identity_and_base_ancestry(tmp_path: Path) -> None:
    subject, base = store(tmp_path)
    head, writer, branch, common = subject.candidate_identity(WORK)
    assert head == base
    assert common == str((subject.repo / ".git").resolve())
    writer.parent.mkdir(parents=True)
    git(subject.repo, "worktree", "add", "-q", "-b", branch, str(writer), base)
    (writer / "candidate").write_text("done\n")
    git(writer, "add", "candidate")
    git(writer, "commit", "-m", "candidate")
    record = WorkerRecord(
        version=1,
        spawn_id=uuid4(),
        work_id=WORK,
        objective="objective",
        writer=str(writer),
        branch=branch,
        base_sha=base,
        unit="unit.service",
        log="/log",
        started_at=1,
        command=("true",),
    )
    candidate, clean, descendant = subject.candidate(record)
    assert candidate != base and clean and descendant

    unrelated = subprocess.check_output(
        ["git", "-C", writer, "commit-tree", git(writer, "write-tree"), "-m", "unrelated"],
        text=True,
    ).strip()
    git(writer, "reset", "--hard", unrelated)
    assert subject.candidate(record) == (unrelated, True, False)


@pytest.mark.parametrize("git_common", [None, "relative/.git", "/tmp/../escape"])
def test_v2_record_requires_absolute_canonical_git_common(git_common: str | None) -> None:
    with pytest.raises(ValueError, match="git_common"):
        WorkerRecord(
            spawn_id=uuid4(), work_id=WORK, objective="objective", writer="/writer",
            branch=f"v2-task-{WORK}", base_sha="a" * 40, git_common=git_common,
            unit="unit.service", log="/log", started_at=1, command=("true",),
        )


def test_v1_record_rejects_git_common() -> None:
    with pytest.raises(ValueError, match="legacy"):
        WorkerRecord(
            version=1, spawn_id=uuid4(), work_id=WORK, objective="objective",
            writer="/writer", branch=f"v2-task-{WORK}", base_sha="a" * 40,
            git_common="/repo/.git", unit="unit.service", log="/log", started_at=1,
            command=("true",),
        )
