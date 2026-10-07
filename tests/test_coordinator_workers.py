from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from switchstand.coordinator_workers import CoordinatorWorkers

WORK = UUID("11111111-1111-4111-8111-111111111111")
SOURCE = Path(__file__).parents[1]


def host_user_systemd_available() -> bool:
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "--machine=marco@.host",
                "show",
                "--property=Version",
                "--value",
            ],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


pytestmark = pytest.mark.skipif(
    not host_user_systemd_available(),
    reason="NOT_RUN: causal worker lifecycle requires the host user-systemd boundary",
)


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", repo, *arguments], text=True).strip()


def repository(tmp_path: Path, gate: Path) -> tuple[Path, Path, Path, str]:
    """Build a hermetic public launcher -> trusted selector -> internal entry chain."""
    home = tmp_path / "home"
    primary = home / "switchstand"
    scripts = primary / "scripts"
    scripts.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", primary, "init", "-b", "main"],
        check=True,
        capture_output=True,
    )
    git(primary, "config", "user.name", "Worker Test")
    git(primary, "config", "user.email", "worker@example.invalid")
    git(primary, "remote", "add", "origin", "https://github.com/marcogallotta/switchstand.git")
    shutil.copy2(SOURCE / "scripts/switchstand", scripts / "switchstand")
    writer = home / ".local/state/switchstand/worktrees" / f"switchstand-task-{WORK}"
    writer.parent.mkdir(parents=True)
    internal = scripts / "switchstand-start"
    internal.write_text(
        "#!/bin/sh\nset -eu\n"
        f'[ "$1" = --active ] && [ "$2" = "{WORK}" ] && [ "$3" = --commit ]\n'
        '[ "$5" = --noninteractive ]\n'
        f'git -C "{primary}" worktree add -q -b "v2-task-{WORK}" "{writer}" "$4"\n'
        f'while [ ! -f "{gate}" ]; do sleep 0.02; done\n'
        f'printf "worker candidate\\n" >"{writer}/worker.txt"\n'
        f'git -C "{writer}" add worker.txt\n'
        f'git -C "{writer}" commit -q -m "isolated candidate" '
        "-m 'Tests: PASS public-isolated-boundary' -m 'Remaining: none'\n"
    )
    internal.chmod(0o755)
    (primary / "tracked.txt").write_text("base\n")
    git(primary, "add", ".")
    git(primary, "commit", "-m", "base")
    base = git(primary, "rev-parse", "HEAD")

    selector = home / ".local/bin/switchstand-start"
    selector.parent.mkdir(parents=True)
    shutil.copy2(SOURCE / "scripts/switchstand-selector", selector)
    controls = home / ".local/state/switchstand/control/controls"
    controls.mkdir(parents=True)
    control = controls / base
    subprocess.run(["git", "clone", "-q", str(primary), str(control)], check=True)
    git(control, "remote", "set-url", "origin", "https://github.com/marcogallotta/switchstand.git")
    git(control, "checkout", "-q", "--detach", base)
    (controls.parent / "manifest").write_text(
        "state=ACTIVE\nrepository=marcogallotta/switchstand\n"
        f"control_sha={base}\ncontrol_path={control}\n"
    )
    return home, primary, writer, base


def manager(home: Path, state: Path) -> CoordinatorWorkers:
    return CoordinatorWorkers(
        home,
        state_root=state,
        runtime_source=SOURCE / "src",
    )


def wait_for(subject: CoordinatorWorkers, spawn_id: UUID, status: str):
    deadline = time.monotonic() + 15
    last = subject.status(spawn_id)
    while time.monotonic() < deadline:
        last = subject.status(spawn_id)
        if last.status == status:
            return last
        if last.status in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.03)
    raise AssertionError(f"worker did not reach {status}: {last}")


def test_public_launch_is_async_isolated_and_restart_safe(tmp_path: Path) -> None:
    gate = tmp_path / "release"
    home, primary, writer, base = repository(tmp_path, gate)
    sibling = tmp_path / "root-writer"
    git(primary, "worktree", "add", "-q", "-b", "root-writer", str(sibling), base)
    state = tmp_path / "worker-state"
    subject = manager(home, state)
    operation = uuid4()

    started_at = time.monotonic()
    started = subject.spawn(operation, WORK, "make the causal change")

    assert time.monotonic() - started_at < 1
    assert started.status == "started" and started.spawn_id == operation
    wait_for(subject, operation, "running")
    assert git(primary, "rev-parse", "HEAD") == base
    assert git(sibling, "status", "--porcelain", "--untracked-files=all") == ""

    gate.touch()
    result = wait_for(subject, operation, "completed")

    assert result.base_sha == base
    assert result.candidate_sha and result.candidate_sha != base
    assert result.writer == str(writer) and result.branch == f"v2-task-{WORK}"
    assert result.summary and "Tests: PASS public-isolated-boundary" in result.summary
    assert "Remaining: none" in result.summary
    assert git(primary, "rev-parse", "HEAD") == base
    assert git(sibling, "status", "--porcelain", "--untracked-files=all") == ""
    recovered = manager(home, state).status(operation)
    assert recovered.status == "completed"
    assert recovered.candidate_sha == result.candidate_sha


def test_replay_conflict_and_cancel_preserve_writer(tmp_path: Path) -> None:
    gate = tmp_path / "release"
    home, _primary, writer, _base = repository(tmp_path, gate)
    state = tmp_path / "worker-state"
    subject = manager(home, state)
    operation = uuid4()
    first = subject.spawn(operation, WORK, "objective")

    assert subject.spawn(operation, WORK, "objective").status == "replayed"
    assert subject.spawn(operation, WORK, "different").reason == "operation_identity_conflict"
    wait_for(subject, operation, "running")
    deadline = time.monotonic() + 5
    while not writer.is_dir() and time.monotonic() < deadline:
        time.sleep(0.03)
    cancelled = subject.cancel(operation)

    assert first.status == "started" and cancelled.status == "cancelled"
    assert subject.status(operation).status == "cancelled"
    assert writer.is_dir()
    assert git(writer, "rev-parse", "--abbrev-ref", "HEAD") == f"v2-task-{WORK}"


def test_cancel_reconciles_lost_running_unit_and_clears_lane(tmp_path: Path) -> None:
    gate = tmp_path / "release"
    home, _primary, writer, _base = repository(tmp_path, gate)
    state = tmp_path / "worker-state"
    subject = manager(home, state)
    operation = uuid4()
    subject.spawn(operation, WORK, "objective")
    wait_for(subject, operation, "running")
    deadline = time.monotonic() + 5
    while not writer.is_dir() and time.monotonic() < deadline:
        time.sleep(0.03)
    assert writer.is_dir()

    subprocess.run(
        [
            "systemctl",
            "--user",
            "--machine=marco@.host",
            "kill",
            "--kill-whom=main",
            "--signal=KILL",
            f"switchstand-implementation-{operation.hex}.service",
        ],
        check=True,
    )

    lost = wait_for(subject, operation, "unknown")
    assert lost.status == "unknown" and lost.reason == "execution_lost"
    assert subject.cancel(operation).status == "cancelled"
    assert subject.status(operation).status == "cancelled"
    assert writer.is_dir()
    assert (state / f"{operation}.log").is_file()

    retry = uuid4()
    assert subject.spawn(retry, WORK, "retry after explicit reconciliation").status == "started"
    failed = wait_for(subject, retry, "failed")
    assert failed.reason == "launcher_failed"


def test_wrong_ancestry_and_corrupt_state_fail_closed(tmp_path: Path) -> None:
    gate = tmp_path / "release"
    home, primary, writer, _base = repository(tmp_path, gate)
    state = tmp_path / "worker-state"
    subject = manager(home, state)
    operation = uuid4()
    subject.spawn(operation, WORK, "objective")
    wait_for(subject, operation, "running")
    gate.touch()
    wait_for(subject, operation, "completed")

    tree = git(writer, "write-tree")
    unrelated = subprocess.check_output(
        ["git", "-C", writer, "commit-tree", tree, "-m", "unrelated"], text=True
    ).strip()
    git(writer, "reset", "--hard", unrelated)
    rejected = subject.status(operation)
    assert rejected.status == "failed"
    assert rejected.reason == "candidate_wrong_ancestry"

    corrupt = state / f"{uuid4()}.json"
    corrupt.write_text("not json\n")
    corrupt.chmod(0o600)
    another = subject.spawn(uuid4(), uuid4(), "another objective")
    assert another.status == "unknown"
    assert another.reason == "ValueError"
    assert git(primary, "status", "--porcelain", "--untracked-files=all") == ""
