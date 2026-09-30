import fcntl
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

import switchstand.stable_auth_migration as migration
from switchstand.stable_auth_migration import (
    WRITER_SERVICES,
    MigrationFailure,
    SystemdWriterProbe,
    copy_with_receipt,
    rollback_receipt,
    state_manifest,
)

SIGNING_MATERIAL = "github-secret-material-with-more-than-32-bytes"


class FixedProbe:
    def __init__(self, stopped: bool = True):
        self.stopped = stopped
        self.calls = 0

    def inactive(self) -> Mapping[str, bool]:
        self.calls += 1
        return {service: self.stopped for service in WRITER_SERVICES}


def state_tree(parent: Path, name: str = "state") -> Path:
    root = parent / name
    root.mkdir(mode=0o700)
    for index, marker in enumerate(migration.STATE_CATEGORIES.values()):
        collection = root / f"S_mcp_{marker}-fixture"
        collection.mkdir(mode=0o700)
        item = collection / f"encrypted-{index}.json"
        item.write_bytes(f"ciphertext-{index}".encode())
        item.chmod(0o600)
    return root


def test_offline_backup_migration_and_explicit_restore_are_exact(tmp_path: Path):
    source = state_tree(tmp_path)
    probe = FixedProbe()
    lock = tmp_path / "migration.lock"
    previous = source
    expected = state_manifest(source)

    for kind in ("backup", "migration", "restore"):
        target = tmp_path / kind
        receipt_path = tmp_path / f"{kind}.receipt.json"
        receipt = copy_with_receipt(
            kind=kind,
            source=previous,
            target=target,
            receipt_path=receipt_path,
            lock_path=lock,
            signing_material=SIGNING_MATERIAL,
            probe=probe,
            clock=lambda: 1234,
        )
        assert state_manifest(target) == expected
        assert receipt["manifest"] == {
            "tree_sha256": expected.tree_sha256,
            "logical_tree_sha256": expected.logical_tree_sha256,
            "files": expected.files,
            "bytes": expected.bytes,
            "category_sha256": expected.category_sha256,
            "logical_category_sha256": expected.logical_category_sha256,
        }
        assert receipt["kind"] == kind
        assert receipt["automatic_oauth_restore"] is False
        assert SIGNING_MATERIAL not in json.dumps(receipt)
        assert receipt_path.stat().st_mode & 0o777 == 0o600
        assert json.loads(receipt_path.read_text()) == receipt
        previous = target
    assert probe.calls == 6


def test_active_or_unknown_writer_blocks_before_copy(tmp_path: Path):
    source = state_tree(tmp_path)
    target = tmp_path / "backup"
    with pytest.raises(MigrationFailure, match="exclusive-writer stop"):
        copy_with_receipt(
            kind="backup",
            source=source,
            target=target,
            receipt_path=tmp_path / "receipt.json",
            lock_path=tmp_path / "migration.lock",
            signing_material=SIGNING_MATERIAL,
            probe=FixedProbe(stopped=False),
        )
    assert not target.exists()


def test_source_change_during_copy_fails_and_preserves_partial_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = state_tree(tmp_path)
    target = tmp_path / "backup"
    original = migration._copy_state  # pyright: ignore[reportPrivateUsage]

    def copy_then_change(copy_source: Path, copy_target: Path):
        original(copy_source, copy_target)
        changed = next(copy_source.rglob("*.json"))
        changed.write_bytes(b"changed-after-copy")

    monkeypatch.setattr(migration, "_copy_state", copy_then_change)
    with pytest.raises(MigrationFailure, match="changed during"):
        copy_with_receipt(
            kind="backup",
            source=source,
            target=target,
            receipt_path=tmp_path / "receipt.json",
            lock_path=tmp_path / "migration.lock",
            signing_material=SIGNING_MATERIAL,
            probe=FixedProbe(),
        )
    assert target.is_dir()
    assert not (tmp_path / "receipt.json").exists()


def test_state_manifest_rejects_symlink_hardlink_and_missing_category(tmp_path: Path):
    source = state_tree(tmp_path)
    first = next(source.rglob("*.json"))
    link = source / "symbolic"
    link.symlink_to(first)
    with pytest.raises(MigrationFailure, match="symbolic"):
        state_manifest(source)
    link.unlink()
    hardlink = source / "hardlink"
    os.link(first, hardlink)
    with pytest.raises(MigrationFailure, match="multiply-linked"):
        state_manifest(source)
    hardlink.unlink()
    first.unlink()
    first.parent.rmdir()
    with pytest.raises(MigrationFailure, match="required encrypted store"):
        state_manifest(source)


def test_rollback_receipt_preserves_current_state_and_rejects_stale_digest(tmp_path: Path):
    state = state_tree(tmp_path)
    manifest = state_manifest(state)
    with pytest.raises(MigrationFailure, match="rollback expectation"):
        rollback_receipt(
            state=state,
            expected_tree_sha256="0" * 64,
            receipt_path=tmp_path / "rollback.json",
            lock_path=tmp_path / "migration.lock",
            signing_material=SIGNING_MATERIAL,
            probe=FixedProbe(),
            clock=lambda: 1234,
        )
    assert not (tmp_path / "rollback.json").exists()
    receipt = rollback_receipt(
        state=state,
        expected_tree_sha256=manifest.tree_sha256,
        receipt_path=tmp_path / "rollback.json",
        lock_path=tmp_path / "migration.lock",
        signing_material=SIGNING_MATERIAL,
        probe=FixedProbe(),
        clock=lambda: 1234,
    )
    assert receipt["action"] == "preserve-current-oauth-state"
    assert receipt["automatic_oauth_restore"] is False
    assert state_manifest(state) == manifest


def test_copy_rejects_existing_nested_or_weakly_bound_targets(tmp_path: Path):
    source = state_tree(tmp_path)
    def copy(target: Path, signing_material: str = SIGNING_MATERIAL):
        return copy_with_receipt(
            kind="backup",
            source=source,
            target=target,
            receipt_path=tmp_path / "receipt.json",
            lock_path=tmp_path / "migration.lock",
            signing_material=signing_material,
            probe=FixedProbe(),
        )

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(MigrationFailure, match="must not exist"):
        copy(existing)
    with pytest.raises(MigrationFailure, match="inside the source"):
        copy(source / "nested")
    with pytest.raises(MigrationFailure, match="signing material"):
        copy(tmp_path / "weak", "x")
    assert not (tmp_path / "weak").exists()


def test_systemd_probe_uses_only_exact_read_only_writer_queries(monkeypatch: pytest.MonkeyPatch):
    commands: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, stdout="ActiveState=inactive\nMainPID=0\n", stderr=""
        )

    monkeypatch.setattr(migration.subprocess, "run", run)
    assert SystemdWriterProbe().inactive() == {service: True for service in WRITER_SERVICES}
    assert [command[3] for command, _kwargs in commands] == list(WRITER_SERVICES)
    assert all(
        command[:4] == ["systemctl", "--user", "show", service]
        for (command, _kwargs), service in zip(commands, WRITER_SERVICES, strict=True)
    )
    assert all(
        kwargs == {
            "check": False,
            "capture_output": True,
            "text": True,
            "timeout": 10,
        }
        for _command, kwargs in commands
    )


def test_signing_material_and_receipt_are_checked_before_copy(tmp_path: Path):
    source = state_tree(tmp_path)
    existing_receipt = tmp_path / "receipt.json"
    existing_receipt.write_text("occupied")
    for secret, receipt in (("x", tmp_path / "new.json"), (SIGNING_MATERIAL, existing_receipt)):
        target = tmp_path / f"target-{len(secret)}-{receipt.name}"
        with pytest.raises(MigrationFailure):
            copy_with_receipt(
                kind="backup",
                source=source,
                target=target,
                receipt_path=receipt,
                lock_path=tmp_path / "migration.lock",
                signing_material=secret,
                probe=FixedProbe(),
            )
        assert not target.exists()


def test_migration_lock_rejects_a_concurrent_transaction(tmp_path: Path):
    source = state_tree(tmp_path)
    lock = tmp_path / "migration.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(MigrationFailure, match="another migration transaction"):
            copy_with_receipt(
                kind="backup",
                source=source,
                target=tmp_path / "target",
                receipt_path=tmp_path / "receipt.json",
                lock_path=lock,
                signing_material=SIGNING_MATERIAL,
                probe=FixedProbe(),
            )
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("conflict", ["receipt-in-source", "lock-in-target", "same-control"])
def test_control_path_conflicts_fail_without_mutation_or_artifacts(
    tmp_path: Path, conflict: str
):
    source = state_tree(tmp_path)
    before = state_manifest(source)
    target = tmp_path / "target"
    receipt = source / "receipt.json" if conflict == "receipt-in-source" else tmp_path / "r.json"
    lock = target / "lock" if conflict == "lock-in-target" else tmp_path / "lock"
    if conflict == "same-control":
        lock = receipt
    with pytest.raises(MigrationFailure, match="must not overlap"):
        copy_with_receipt(
            kind="migration",
            source=source,
            target=target,
            receipt_path=receipt,
            lock_path=lock,
            signing_material=SIGNING_MATERIAL,
            probe=FixedProbe(),
        )
    assert state_manifest(source) == before
    assert not target.exists()
    assert not receipt.exists()
    assert not lock.exists()


def test_receipt_partial_write_failure_cleans_temporary_and_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    receipt = tmp_path / "receipt.json"
    original = migration.os.write
    calls = 0

    def fail_after_partial(descriptor: int, value: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(descriptor, value[:1])
        raise OSError("injected partial-write failure")

    monkeypatch.setattr(migration.os, "write", fail_after_partial)
    with pytest.raises(MigrationFailure, match="publication failed"):
        migration._write_receipt(receipt, {"result": "PASS"})  # pyright: ignore[reportPrivateUsage]
    assert not receipt.exists()
    assert not list(tmp_path.glob(".receipt.json.*.tmp"))


@pytest.mark.parametrize("fail_call", [1, 2])
def test_receipt_file_or_parent_fsync_failure_leaves_no_published_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_call: int
):
    receipt = tmp_path / "receipt.json"
    original = migration.os.fsync
    calls = 0

    def fail_selected(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == fail_call:
            raise OSError("injected fsync failure")
        original(descriptor)

    monkeypatch.setattr(migration.os, "fsync", fail_selected)
    with pytest.raises(MigrationFailure, match="publication failed"):
        migration._write_receipt(receipt, {"result": "PASS"})  # pyright: ignore[reportPrivateUsage]
    assert not receipt.exists()
    assert not list(tmp_path.glob(".receipt.json.*.tmp"))


def test_rollback_rechecks_stable_state_before_publishing_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state = state_tree(tmp_path)
    expected = state_manifest(state)
    original = migration.state_manifest
    calls = 0

    def mutate_between_checks(root: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            next(root.rglob("*.json")).write_bytes(b"changed-during-rollback")
        return original(root)

    monkeypatch.setattr(migration, "state_manifest", mutate_between_checks)
    receipt = tmp_path / "rollback.json"
    probe = FixedProbe()
    with pytest.raises(MigrationFailure, match="changed during rollback"):
        rollback_receipt(
            state=state,
            expected_tree_sha256=expected.tree_sha256,
            receipt_path=receipt,
            lock_path=tmp_path / "lock",
            signing_material=SIGNING_MATERIAL,
            probe=probe,
        )
    assert probe.calls == 2
    assert not receipt.exists()
