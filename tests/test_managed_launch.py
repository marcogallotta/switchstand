import inspect
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from switchstand.agent_broker import Broker, Budget, Pressure
from switchstand.agent_executor import ManagedExecutor
from switchstand.managed_launch import PreparedLaunchStore

WORK_ID = UUID("11111111-1111-4111-8111-111111111111")
GRANT_ID = UUID("22222222-2222-4222-8222-222222222222")
GREEN = Pressure(16_000, 12_000, 4_000, 3_900, 0, swap_activity_pages=0)


def prepared(tmp_path: Path) -> tuple[Broker, Path]:
    root = tmp_path / "broker"
    broker = Broker(root)
    broker.initialize(Budget(4096, 6144, 512, 400, 512, 4, 1))
    request = root / "inboxes/root/request.json"
    request.write_text(
        json.dumps(
            {
                "request_id": "request",
                "parent": "root",
                "worker": "managed-parent",
                "worker_class": "light",
                "children": {
                    "memory_high_mib": 700,
                    "memory_max_mib": 1024,
                    "swap_max_mib": 128,
                    "cpu_percent": 100,
                    "tasks": 96,
                    "workers": 1,
                    "heavy": 0,
                },
            }
        )
    )
    request.chmod(0o600)
    reservation = broker.ingest("root", "request", GREEN)
    control = tmp_path / "control"
    writer = tmp_path / "writer"
    codex_home = tmp_path / "codex-home"
    control.mkdir(mode=0o755)
    writer.mkdir(mode=0o700)
    codex_home.mkdir(mode=0o700)
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    manifest = PreparedLaunchStore(broker).prepare(
        work_id=WORK_ID,
        grant_id=GRANT_ID,
        grant_version=7,
        lease_id="managed-parent",
        reservation_id=reservation["reservation_id"],
        control=control,
        writer=writer,
        codex_home=codex_home,
        codex_executable=executable,
        assignment="implement the exact task",
    )
    return broker, manifest


def test_prepared_manifest_is_exact_sealed_and_has_only_canonical_command(tmp_path: Path) -> None:
    broker, path = prepared(tmp_path)
    manifest = PreparedLaunchStore(broker).load(path)

    assert manifest.work_id == WORK_ID
    assert manifest.grant_id == GRANT_ID
    assert manifest.grant_version == 7
    assert manifest.reservation_id == broker.lease("managed-parent")["reservation_id"]
    assert manifest.command[:4] == (str(tmp_path / "codex"), "exec", "-C", str(tmp_path / "writer"))
    assert "--dangerously-bypass-approvals-and-sandbox" not in manifest.command
    assert "--json" in manifest.command
    assert "command" not in inspect.signature(PreparedLaunchStore.prepare).parameters


def test_manifest_tampering_and_wrong_store_are_rejected(tmp_path: Path) -> None:
    broker, path = prepared(tmp_path)
    envelope = json.loads(path.read_text())
    envelope["payload"]["grant_version"] = 8
    path.write_text(json.dumps(envelope))
    path.chmod(0o600)

    with pytest.raises(ValueError, match="seal mismatch"):
        PreparedLaunchStore(broker).load(path)
    with pytest.raises(ValueError, match="outside"):
        PreparedLaunchStore(broker).load(tmp_path / "other.json")


def test_private_launch_paths_and_exact_reservation_are_required(tmp_path: Path) -> None:
    broker, _ = prepared(tmp_path)
    store = PreparedLaunchStore(broker)
    writer = tmp_path / "unsafe-writer"
    writer.mkdir(mode=0o755)

    values = {
        "work_id": WORK_ID,
        "grant_id": GRANT_ID,
        "grant_version": 7,
        "lease_id": "managed-parent",
        "control": tmp_path / "control",
        "codex_home": tmp_path / "codex-home",
        "codex_executable": tmp_path / "codex",
        "assignment": "task",
    }
    with pytest.raises(ValueError, match="reservation identity"):
        store.prepare(
            reservation_id="wrong",
            writer=tmp_path / "writer",
            **values,
        )
    with pytest.raises(PermissionError, match="unsafe"):
        store.prepare(
            reservation_id=broker.lease("managed-parent")["reservation_id"],
            writer=writer,
            **values,
        )


def test_managed_executor_uses_aggregate_budget_and_exact_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker, path = prepared(tmp_path)
    calls: list[list[str]] = []

    def run(arguments: list[str], **_: object) -> SimpleNamespace:
        calls.append(arguments)
        if "show" in arguments:
            return SimpleNamespace(returncode=0, stdout="inactive\n\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    receipt = ManagedExecutor(broker).run(path, timeout=60)

    assert receipt["state"] == "completed"
    assert broker.status()["leases"]["managed-parent"]["state"] == "completed"
    start = calls[0]
    assert "--property=MemoryMax=2048M" in start
    assert f"--property=BindReadOnlyPaths={tmp_path / 'control'}" in start
    assert f"--property=BindPaths={tmp_path / 'writer'}" in start
    assert f"--property=BindPaths={tmp_path / 'codex-home'}" in start
    assert f"ACTIVE_WORK_ID={WORK_ID}" in start
    assert f"SWITCHSTAND_GRANT_ID={GRANT_ID}" in start
    separator = start.index("--")
    assert start[separator + 1 : separator + 4] == [
        "/usr/bin/env",
        "-i",
        f"HOME={tmp_path / 'codex-home'}",
    ]
    assert str(tmp_path / "codex") in start[separator:]


def test_managed_timeout_releases_only_after_terminal_empty_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker, path = prepared(tmp_path)

    def run(arguments: list[str], **_: object) -> SimpleNamespace:
        if arguments[0] == "systemd-run":
            raise subprocess.TimeoutExpired(arguments, 60)
        if "show" in arguments:
            return SimpleNamespace(returncode=0, stdout="inactive\n\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    receipt = ManagedExecutor(broker).run(path, timeout=60)

    assert receipt["state"] == "timed_out"
    assert broker.status()["leases"]["managed-parent"]["state"] == "cancelled"


def test_managed_executor_preserves_reservation_when_runtime_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker, path = prepared(tmp_path)

    def run(arguments: list[str], **_: object) -> SimpleNamespace:
        if "show" in arguments:
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(subprocess, "run", run)
    receipt = ManagedExecutor(broker).run(path, timeout=60)

    assert receipt["state"] == "unknown"
    assert broker.lease("managed-parent")["state"] == "execution_active"
