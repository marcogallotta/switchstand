"""Tests for the production edge replacement transaction."""
# pyright: reportPrivateUsage=false

import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

import switchstand.edge_maintenance as maintenance
from switchstand.edge_maintenance import (
    FASTMCP_STATE,
    Config,
    Failed,
    HostOperations,
    Interrupted,
    Unknown,
    _validate_launch_mapping,
    deploy,
    exclusive_lock,
    recover_upgrade_no_effect,
    validate_target,
)
from switchstand.edge_monitor import HttpObservation


def config(
    tmp_path: Path,
    *,
    caddy: str = maintenance.CADDY,
    public_origin: str = maintenance.PUBLIC_ORIGIN,
) -> Config:
    attempt = tmp_path / "attempt"
    attempt.mkdir(parents=True)
    return Config(
        attempt_dir=attempt,
        current_runtime=tmp_path / "current-runtime",
        current_sha="d" * 40,
        candidate_runtime=tmp_path / "runtime",
        candidate_sha="a" * 40,
        candidate_launcher=tmp_path / "candidate-launcher",
        candidate_launcher_sha="b" * 64,
        launcher=tmp_path / "launcher",
        current_launcher_sha="c" * 64,
        fastmcp_state=FASTMCP_STATE,
        env_file=tmp_path / "edge.env",
        target="production",
        caddy=caddy,
        public_origin=public_origin,
    )


class FakeOperations:
    def __init__(
        self,
        *,
        public_gate: bool = True,
        current_public: bool = True,
        local: bool = True,
        public: bool = True,
        fail_at: str | None = None,
        unknown_at: str | None = None,
        rollback: bool = True,
        rollback_complete: bool = True,
        upgrade: str = "APPLIED",
        semantic: bool = True,
    ):
        self.events: list[str] = []
        self.gated = False
        self.public_gate = public_gate
        self.current_public = current_public
        self.local = local
        self.public = public
        self.fail_at = fail_at
        self.unknown_at = unknown_at
        self.rollback = rollback
        self.complete = rollback_complete
        self.upgrade = upgrade
        self.semantic = semantic

    def _event(self, name: str) -> None:
        self.events.append(name)
        if self.fail_at == name:
            raise Failed("secret must never reach receipt")
        if self.unknown_at == name:
            raise Unknown("ambiguous")

    def preflight(self):
        self._event("preflight")

    def gate(self):
        self._event("gate")
        self.gated = True

    def gate_exact(self):
        self._event("gate_exact")
        return self.gated

    def public_gated(self):
        self._event("public_gated")
        return self.public_gate

    def stop(self):
        self._event("stop")

    def upgrade_state(self):
        self._event("upgrade_state")
        return self.upgrade

    def snapshot(self):
        self._event("snapshot")

    def snapshot_digest(self):
        return "a" * 64

    def reconcile_phase(self, phase, proof):
        self._event("reconcile_" + phase.lower())
        return phase, {}

    def swap(self):
        self._event("swap")

    def start(self):
        self._event("start")

    def local_ready(self):
        self._event("local_ready")
        return self.local

    def semantic_ready(self):
        self._event("semantic_ready")
        if not self.semantic:
            raise Failed("semantic proof failed")
        return "e" * 64

    def rollback_ready(self):
        self._event("rollback_ready")
        return self.rollback

    def rollback_complete(self):
        self._event("rollback_complete")
        return self.complete

    def gate_abort_ready(self):
        self._event("gate_abort_ready")
        return self.rollback

    def current_public_ready(self):
        self._event("current_public_ready")
        return self.current_public

    def ungate(self):
        self._event("ungate")
        self.gated = False

    def public_ready(self):
        self._event("public_ready")
        return self.public

    def restore_launcher(self):
        self._event("restore_launcher")

    def prove_upgrade_no_effect(self):
        self._event("prove_upgrade_no_effect")


def receipt(subject: Config) -> dict[str, object]:
    return json.loads((subject.attempt_dir / "receipt.json").read_text())


def seed_receipt(subject: Config, phase: str) -> None:
    durable = maintenance.Receipt(subject)
    if phase == "UPGRADE_PENDING":
        durable.value["state_upgrade"] = "PENDING"
    elif phase not in {"PREFLIGHT", "GATED", "STOPPED", "ROLLED_BACK"}:
        durable.value["state_upgrade"] = "APPLIED"
    if phase not in {"PREFLIGHT", "GATED", "STOPPED", "UPGRADE_PENDING", "UPGRADED"}:
        durable.value["fastmcp_snapshot"] = "a" * 64
    durable.write(phase, "UNKNOWN")


def resume_subject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    subject = config(tmp_path)
    subject.attempt_dir.chmod(0o700)
    subject.current_runtime.mkdir()
    subject.candidate_runtime.mkdir()
    candidate, current = b"candidate launcher", b"current launcher"
    for path, data, mode in (
        (subject.candidate_launcher, candidate, 0o700),
        (subject.launcher, current, 0o700),
        (subject.env_file, b"ASANA_TOKEN=test\n", 0o600),
    ):
        path.write_bytes(data)
        path.chmod(mode)
    subject = replace(
        subject,
        candidate_launcher_sha=hashlib.sha256(candidate).hexdigest(),
        current_launcher_sha=hashlib.sha256(current).hexdigest(),
    )
    state = {"dirty": False}

    def run(command, **_kwargs):
        if command[0] == "git":
            runtime = Path(command[2])
            if command[3] == "status":
                output = "dirty\n" if state["dirty"] else ""
            else:
                sha = subject.current_sha if runtime == subject.current_runtime else subject.candidate_sha
                output = sha + "\n"
        else:
            output = str(subject.launcher) + "\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(maintenance, "run_host_command", run)
    return subject, HostOperations(subject), state


@pytest.mark.parametrize(
    ("phase", "gate", "service", "expected"),
    [
        ("PREFLIGHT", "APPLIED", "ACTIVE", "GATED"),
        ("GATED", "APPLIED", "INACTIVE", "STOPPED"),
        ("UPGRADED", "APPLIED", "INACTIVE", "SNAPSHOTTED"),
        ("SNAPSHOTTED", "APPLIED", "INACTIVE", "SWAPPED"),
        ("SWAPPED", "APPLIED", "ACTIVE", "STARTED"),
        ("STARTED", "ABSENT", "ACTIVE", "UNGATED"),
    ],
)
def test_reconcile_classifies_effect_completed_before_phase_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    phase: str, gate: str, service: str, expected: str,
):
    subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    monkeypatch.setattr(operations, "_gate_state", lambda: gate)
    monkeypatch.setattr(operations, "_service_state", lambda: service)
    monkeypatch.setattr(operations, "public_gated", lambda: True)
    monkeypatch.setattr(operations, "local_ready", lambda: True)
    monkeypatch.setattr(operations, "public_ready", lambda: True)
    proof: dict[str, object] = {}
    if phase in {"UPGRADED", "SNAPSHOTTED", "SWAPPED", "STARTED"}:
        operations.snapshot_file.write_bytes(b"snapshot")
        operations.snapshot_file.chmod(0o600)
    if phase in {"SNAPSHOTTED", "SWAPPED", "STARTED"}:
        proof["fastmcp_snapshot"] = hashlib.sha256(b"snapshot").hexdigest()
    if phase in {"SNAPSHOTTED", "SWAPPED", "STARTED"}:
        operations.backup.write_bytes(b"current launcher")
        operations.backup.chmod(0o600)
        subject.launcher.write_bytes(b"candidate launcher")

    observed, added = operations.reconcile_phase(phase, proof)

    assert observed == expected
    if phase == "STOPPED":
        assert added["fastmcp_snapshot"] == hashlib.sha256(b"snapshot").hexdigest()


def test_reconcile_preserves_ungated_phase_when_safety_gate_was_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    monkeypatch.setattr(operations, "_gate_state", lambda: "APPLIED")
    monkeypatch.setattr(operations, "_service_state", lambda: "ACTIVE")
    monkeypatch.setattr(operations, "local_ready", lambda: True)
    operations.snapshot_file.write_bytes(b"snapshot")
    operations.snapshot_file.chmod(0o600)
    operations.backup.write_bytes(b"current launcher")
    operations.backup.chmod(0o600)
    subject.launcher.write_bytes(b"candidate launcher")

    phase, proof = operations.reconcile_phase(
        "UNGATED",
        {"fastmcp_snapshot": hashlib.sha256(b"snapshot").hexdigest()},
    )

    assert phase == "UNGATED"
    assert proof == {"gate_retained": "true"}


def test_reconcile_normalizes_caddy_gate_http_500_to_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    _subject, operations, _state = resume_subject(tmp_path, monkeypatch)

    def unavailable(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://caddy/id/gate", 500, "error", None, None)

    monkeypatch.setattr(maintenance.urllib.request, "urlopen", unavailable)

    with pytest.raises(Unknown, match="gate readback failed"):
        operations.reconcile_phase("PREFLIGHT", {})


@pytest.mark.parametrize(
    "fault",
    [
        "attempt-mode", "attempt-link", "relative", "wrong-owner",
        "launcher-link", "launcher-loop", "launcher-digest", "dirty", "git-read",
    ],
)
def test_resume_trust_rejects_host_identity_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
):
    subject, operations, state = resume_subject(tmp_path, monkeypatch)
    if fault == "attempt-mode":
        subject.attempt_dir.chmod(0o755)
    elif fault == "attempt-link":
        target = tmp_path / "real-attempt"
        subject.attempt_dir.replace(target)
        subject.attempt_dir.symlink_to(target, target_is_directory=True)
    elif fault == "relative":
        subject = replace(subject, attempt_dir=Path("attempt"))
        operations = HostOperations(subject)
    elif fault == "wrong-owner":
        uid = os.getuid()
        monkeypatch.setattr(maintenance.os, "getuid", lambda: uid + 1)
    elif fault == "launcher-link":
        subject.candidate_launcher.unlink()
        subject.candidate_launcher.symlink_to(subject.launcher)
    elif fault == "launcher-loop":
        subject.candidate_launcher.unlink()
        subject.candidate_launcher.symlink_to(subject.candidate_launcher)
    elif fault == "launcher-digest":
        subject.candidate_launcher.write_bytes(b"changed")
    elif fault == "git-read":
        monkeypatch.setattr(
            maintenance,
            "run_host_command",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                subprocess.CalledProcessError(1, ["git"])
            ),
        )
    else:
        state["dirty"] = True
    with pytest.raises(Unknown):
        operations.reconcile_phase("PREFLIGHT", {})


@pytest.mark.parametrize(
    "artifact",
    [
        "selected-launcher", "selected-digest", "snapshot", "snapshot-link",
        "snapshot-digest", "backup", "backup-link", "backup-digest",
    ],
)
def test_resume_trust_rejects_changed_phase_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str,
):
    subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    monkeypatch.setattr(operations, "_gate_state", lambda: "APPLIED")
    monkeypatch.setattr(operations, "_service_state", lambda: "INACTIVE")
    monkeypatch.setattr(operations, "public_gated", lambda: True)
    operations.snapshot_file.write_bytes(b"snapshot")
    operations.snapshot_file.chmod(0o600)
    proof = {"fastmcp_snapshot": hashlib.sha256(b"snapshot").hexdigest()}
    phase = "SNAPSHOTTED"
    if artifact == "selected-launcher":
        subject.launcher.chmod(0o755)
    elif artifact == "selected-digest":
        subject.launcher.write_bytes(b"changed")
    elif artifact == "snapshot":
        operations.snapshot_file.chmod(0o644)
    elif artifact == "snapshot-link":
        target = tmp_path / "other-snapshot"
        operations.snapshot_file.replace(target)
        operations.snapshot_file.symlink_to(target)
    elif artifact == "snapshot-digest":
        proof["fastmcp_snapshot"] = "0" * 64
    else:
        phase = "SNAPSHOTTED"
        operations.backup.write_bytes(b"current launcher")
        operations.backup.chmod(
            0o600 if artifact in {"backup-link", "backup-digest"} else 0o644
        )
        if artifact == "backup-link":
            target = tmp_path / "other-backup"
            operations.backup.replace(target)
            operations.backup.symlink_to(target)
        elif artifact == "backup-digest":
            operations.backup.write_bytes(b"changed")
        subject.launcher.write_bytes(b"candidate launcher")
    with pytest.raises(Unknown):
        operations.reconcile_phase(phase, proof)


def test_success_gates_every_public_path_before_stop(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations()

    assert deploy(subject, operations) == "PASS"

    assert operations.events == [
        "preflight",
        "current_public_ready",
        "gate",
        "public_gated",
        "stop",
        "upgrade_state",
        "snapshot",
        "swap",
        "start",
        "local_ready",
        "ungate",
        "public_ready",
    ]
    assert receipt(subject)["status"] == "PASS"


@pytest.mark.parametrize(
    ("phase", "observed", "completed"),
    [
        ("PREFLIGHT", "GATED", "gate"),
        ("GATED", "STOPPED", "stop"),
        ("STOPPED", "UPGRADED", "upgrade_state"),
        ("UPGRADED", "SNAPSHOTTED", "snapshot"),
        ("SNAPSHOTTED", "SWAPPED", "swap"),
        ("SWAPPED", "STARTED", "start"),
        ("STARTED", "UNGATED", "ungate"),
    ],
)
def test_resume_never_replays_a_proven_completed_host_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    phase: str, observed: str, completed: str,
):
    subject = config(tmp_path)
    seed_receipt(subject, phase)
    operations = FakeOperations()
    proof = {"fastmcp_snapshot": "a" * 64} if phase == "STOPPED" else {}
    monkeypatch.setattr(
        operations, "reconcile_phase", lambda *_args, **_kwargs: (observed, proof)
    )

    assert deploy(subject, operations) == "PASS"
    assert completed not in operations.events


def test_malformed_receipt_installs_and_proves_gate_before_unknown(tmp_path: Path):
    subject = config(tmp_path)
    path = subject.attempt_dir / "receipt.json"
    path.write_text("{")
    path.chmod(0o600)
    operations = FakeOperations()

    assert deploy(subject, operations) == "UNKNOWN"
    assert operations.gated
    assert operations.events == ["gate_exact", "gate", "gate_exact", "public_gated"]


def test_receipt_cannot_inject_retained_gate_proof(tmp_path: Path):
    subject = config(tmp_path)
    seed_receipt(subject, "STARTED")
    path = subject.attempt_dir / "receipt.json"
    value = json.loads(path.read_text())
    value["gate_retained"] = "true"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    operations = FakeOperations()

    assert deploy(subject, operations) == "UNKNOWN"
    assert "ungate" not in operations.events
    assert operations.gated


def test_gate_retention_reports_its_own_unknown_class():
    operations = FakeOperations(unknown_at="gate")
    with pytest.raises(maintenance.GateRetentionUnknown):
        maintenance.retain_gate(operations)


def test_unproved_public_gate_restores_old_route_without_stopping(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(public_gate=False, public=False)

    assert deploy(subject, operations) == "FAIL"

    assert "stop" not in operations.events
    assert "upgrade_state" not in operations.events
    assert "swap" not in operations.events
    assert operations.events[-4:] == [
        "gate_abort_ready", "ungate", "gate_exact", "public_ready",
    ]
    assert not operations.gated
    assert {key: receipt(subject)[key] for key in ("phase", "status", "error")} == {
        "phase": "ROLLED_BACK",
        "status": "FAIL",
        "error": "PublicGateUnproven",
    }


def test_unproved_public_gate_ambiguous_ungate_does_not_reinstall_gate(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(public_gate=False, unknown_at="ungate")

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.events[-2:] == ["gate_abort_ready", "ungate"]
    assert operations.events.count("gate") == 1
    assert "stop" not in operations.events
    assert {key: receipt(subject)[key] for key in ("phase", "status", "error")} == {
        "phase": "GATED",
        "status": "UNKNOWN",
        "error": "GateAbortUnknown",
    }


def test_preflight_public_failure_never_installs_gate(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(current_public=False)

    assert deploy(subject, operations) == "FAIL"

    assert operations.events == ["preflight", "current_public_ready"]
    assert not operations.gated
    assert receipt(subject)["phase"] == "ROLLED_BACK"


def test_interrupted_preflight_gate_routes_to_abort_without_reinstall(tmp_path: Path):
    subject = config(tmp_path)
    seed_receipt(subject, "PREFLIGHT")
    operations = FakeOperations(public_gate=False, public=False)
    operations.gated = True
    operations.reconcile_phase = lambda _phase, _proof: ("GATED", {})  # type: ignore[method-assign]

    assert deploy(subject, operations) == "FAIL"

    assert "stop" not in operations.events
    assert "upgrade_state" not in operations.events
    assert "swap" not in operations.events
    assert operations.events.count("gate") == 0
    assert not operations.gated
    assert receipt(subject)["phase"] == "ROLLED_BACK"


def test_preflight_reconcile_defers_external_gate_proof_to_abort_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    subject = config(tmp_path)
    operations = HostOperations(subject)
    monkeypatch.setattr(operations, "_resume_trust", lambda: None)
    monkeypatch.setattr(operations, "_gate_state", lambda: "APPLIED")
    monkeypatch.setattr(operations, "_service_state", lambda: "ACTIVE")
    monkeypatch.setattr(
        operations,
        "_artifact_digest",
        lambda path, _mode: subject.current_launcher_sha
        if path == subject.launcher
        else "unexpected",
    )
    monkeypatch.setattr(
        operations,
        "public_gated",
        lambda: pytest.fail("GATED branch owns external proof and abort"),
    )

    assert operations.reconcile_phase("PREFLIGHT", {}) == ("GATED", {})


def test_gate_abort_unknown_reentry_reconciles_without_reinstall(tmp_path: Path):
    subject = config(tmp_path)
    seed_receipt(subject, "GATED")
    value = receipt(subject)
    value["error"] = "GateAbortUnknown"
    subject.attempt_dir.joinpath("receipt.json").write_text(json.dumps(value))
    subject.attempt_dir.joinpath("receipt.json").chmod(0o600)
    operations = FakeOperations()
    operations.gated = True

    assert deploy(subject, operations) == "FAIL"

    assert operations.events == [
        "gate_abort_ready", "ungate", "gate_exact", "public_ready",
    ]
    assert not operations.gated
    assert receipt(subject)["phase"] == "ROLLED_BACK"


def test_gate_abort_unknown_reentry_requires_full_attempt_trust(tmp_path: Path):
    subject = config(tmp_path)
    seed_receipt(subject, "GATED")
    value = receipt(subject)
    value["error"] = "GateAbortUnknown"
    subject.attempt_dir.joinpath("receipt.json").write_text(json.dumps(value))
    subject.attempt_dir.joinpath("receipt.json").chmod(0o600)
    operations = FakeOperations(unknown_at="gate_abort_ready")
    operations.gated = True

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.events == ["gate_abort_ready"]
    assert operations.gated
    assert receipt(subject)["error"] == "GateAbortUnknown"


def test_host_gate_abort_readiness_validates_resume_trust_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    operations = HostOperations(config(tmp_path))
    monkeypatch.setattr(
        operations,
        "_resume_trust",
        lambda: (_ for _ in ()).throw(Unknown("changed candidate runtime")),
    )
    monkeypatch.setattr(
        operations,
        "rollback_ready",
        lambda: pytest.fail("rollback doctor must not run before attempt trust"),
    )

    with pytest.raises(Unknown, match="changed candidate runtime"):
        operations.gate_abort_ready()


def test_definite_pre_upgrade_failure_restarts_old_runtime_and_ungates(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(fail_at="stop")

    assert deploy(subject, operations) == "FAIL"

    assert operations.events[-3:] == ["stop", "ungate", "public_ready"]
    assert not operations.gated
    assert receipt(subject)["phase"] == "ROLLED_BACK"
    assert receipt(subject)["state_upgrade"] == "NOT_STARTED"


def test_candidate_failure_after_state_upgrade_keeps_gate_and_reports_unknown(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(local=False)

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.gated
    assert "restore_launcher" not in operations.events
    assert "ungate" not in operations.events
    assert receipt(subject)["phase"] == "STARTED"


def test_no_effect_candidate_semantic_failure_restores_complete_old_basis(
    tmp_path: Path,
):
    subject = config(tmp_path)
    operations = FakeOperations(upgrade="NO_EFFECT", semantic=False)

    assert deploy(subject, operations) == "FAIL"

    assert operations.events[-8:] == [
        "semantic_ready", "stop", "restore_launcher", "start",
        "rollback_ready", "ungate", "public_ready", "rollback_complete",
    ]
    assert not operations.gated
    assert {key: receipt(subject)[key] for key in (
        "phase", "status", "state_upgrade",
    )} == {"phase": "ROLLED_BACK", "status": "FAIL", "state_upgrade": "NO_EFFECT"}


def test_no_effect_partial_restoration_never_reports_rolled_back(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(
        upgrade="NO_EFFECT", semantic=False, rollback_complete=False,
    )

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.events[-1] == "public_gated"
    assert operations.gated
    assert receipt(subject)["error"] == "RollbackUnknown"


def test_no_effect_success_requires_and_records_governing_semantic_proof(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(upgrade="NO_EFFECT")

    assert deploy(subject, operations) == "PASS"

    assert "semantic_ready" in operations.events
    assert receipt(subject)["semantic_probe"] == "e" * 64


def test_no_effect_changed_semantic_proof_is_unknown_without_blind_activation(
    tmp_path: Path,
):
    subject = config(tmp_path)
    operations = FakeOperations(upgrade="NO_EFFECT")
    assert deploy(subject, operations) == "PASS"
    value = receipt(subject)
    value["status"] = "RUNNING"
    value["phase"] = "STARTED"
    subject.attempt_dir.joinpath("receipt.json").write_text(json.dumps(value))
    subject.attempt_dir.joinpath("receipt.json").chmod(0o600)
    operations = FakeOperations(upgrade="NO_EFFECT")
    operations.semantic_ready = lambda: "f" * 64  # type: ignore[method-assign]

    assert deploy(subject, operations) == "UNKNOWN"
    assert "ungate" not in operations.events


def test_rollback_ambiguity_keeps_maintenance_gate_and_reports_unknown(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(local=False, rollback=False)

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.gated
    assert receipt(subject)["error"] == "RollbackUnknown"


def test_ambiguous_state_upgrade_retains_gate_without_retry_or_launcher_effects(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(unknown_at="upgrade_state")

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.gated
    assert "swap" not in operations.events
    assert "start" not in operations.events
    assert "restore_launcher" not in operations.events
    assert receipt(subject)["phase"] == "UPGRADE_PENDING"
    assert receipt(subject)["state_upgrade"] == "PENDING"


def test_pending_state_upgrade_receipt_never_retries_after_reentry(tmp_path: Path):
    subject = config(tmp_path)
    seed_receipt(subject, "UPGRADE_PENDING")
    operations = FakeOperations()

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.gated
    assert "upgrade_state" not in operations.events
    assert "swap" not in operations.events


def test_recovery_terminalizes_only_exact_pending_upgrade_as_no_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    subject = config(tmp_path)
    subject = replace(subject, lock_path=tmp_path / "edge.lock")
    seed_receipt(subject, "UPGRADE_PENDING")
    monkeypatch.setattr(maintenance, "LOCK", subject.lock_path)
    monkeypatch.setattr(maintenance, "STATE_UPGRADE_LOCK", tmp_path / "state-upgrade.lock")
    operations = FakeOperations()

    assert recover_upgrade_no_effect(subject, operations) == "FAIL"

    assert operations.events == ["prove_upgrade_no_effect"]
    assert receipt(subject)["error"] == "UpgradeProvenNotApplied"
    assert receipt(subject)["phase"] == "NO_EFFECT"
    assert receipt(subject)["status"] == "FAIL"
    assert receipt(subject)["state_upgrade"] == "NOT_STARTED"
    assert not maintenance.STATE_UPGRADE_LOCK.exists()
    assert maintenance.Receipt(subject).value["phase"] == "NO_EFFECT"
    recorded = subject.attempt_dir.joinpath("receipt.json").read_bytes()
    replay = FakeOperations()
    assert recover_upgrade_no_effect(subject, replay) == "FAIL"
    assert replay.events == []
    assert subject.attempt_dir.joinpath("receipt.json").read_bytes() == recorded


@pytest.mark.parametrize("phase", ["PREFLIGHT", "STOPPED", "UPGRADED"])
def test_recovery_refuses_any_other_receipt_without_proof_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
):
    subject = replace(config(tmp_path), lock_path=tmp_path / "edge.lock")
    seed_receipt(subject, phase)
    before = receipt(subject)
    monkeypatch.setattr(maintenance, "LOCK", subject.lock_path)
    monkeypatch.setattr(maintenance, "STATE_UPGRADE_LOCK", tmp_path / "state-upgrade.lock")
    operations = FakeOperations()

    assert recover_upgrade_no_effect(subject, operations) == "UNKNOWN"

    assert operations.events == []
    assert receipt(subject) == before


def test_recovery_refuses_active_or_stale_state_upgrade_lock_without_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    subject = replace(config(tmp_path), lock_path=tmp_path / "edge.lock")
    seed_receipt(subject, "UPGRADE_PENDING")
    before = receipt(subject)
    monkeypatch.setattr(maintenance, "LOCK", subject.lock_path)
    state_lock = tmp_path / "state-upgrade.lock"
    state_lock.mkdir()
    monkeypatch.setattr(maintenance, "STATE_UPGRADE_LOCK", state_lock)
    operations = FakeOperations()

    assert recover_upgrade_no_effect(subject, operations) == "UNKNOWN"

    assert operations.events == []
    assert receipt(subject) == before


def test_recovery_refuses_receipt_change_during_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    subject = replace(config(tmp_path), lock_path=tmp_path / "edge.lock")
    seed_receipt(subject, "UPGRADE_PENDING")
    before = receipt(subject)
    monkeypatch.setattr(maintenance, "LOCK", subject.lock_path)
    monkeypatch.setattr(maintenance, "STATE_UPGRADE_LOCK", tmp_path / "state-upgrade.lock")

    class RacingOperations(FakeOperations):
        def prove_upgrade_no_effect(self):
            super().prove_upgrade_no_effect()
            changed = receipt(subject)
            changed["error"] = "changed-concurrently"
            subject.attempt_dir.joinpath("receipt.json").write_text(json.dumps(changed))

    operations = RacingOperations()

    assert recover_upgrade_no_effect(subject, operations) == "UNKNOWN"

    assert operations.events == ["prove_upgrade_no_effect"]
    assert receipt(subject) != before
    assert receipt(subject)["phase"] == "UPGRADE_PENDING"


def test_definite_post_upgrade_failure_keeps_gate_and_does_not_restart_old_runtime(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(fail_at="snapshot")

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.gated
    assert "restore_launcher" not in operations.events
    assert "start" not in operations.events
    assert "ungate" not in operations.events
    assert receipt(subject)["state_upgrade"] == "APPLIED"


def test_post_upgrade_candidate_failure_retains_gate_after_local_check(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(local=False, unknown_at="public_ready")

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.events[-3:] == ["gate_exact", "gate_exact", "public_gated"]
    assert operations.gated
    assert receipt(subject)["error"] == "RollbackUnknown"


def test_ambiguous_public_readback_reinstalls_gate(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(public=False)

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.events[-4:] == ["gate_exact", "gate", "gate_exact", "public_gated"]
    assert operations.gated


def test_receipt_redacts_failure_detail(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(fail_at="preflight")

    assert deploy(subject, operations) == "FAIL"
    rendered = (subject.attempt_dir / "receipt.json").read_text()
    assert "secret" not in rendered
    assert receipt(subject)["error"] == "Failed"


def test_ambiguous_preflight_does_not_create_a_gate(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(unknown_at="preflight")

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.events == ["preflight"]
    assert not operations.gated


def test_rollback_restores_launcher_without_rewinding_oauth_state(tmp_path: Path):
    subject = replace(config(tmp_path), fastmcp_state=tmp_path / "fastmcp")
    subject.launcher.write_text("candidate")
    old = subject.attempt_dir / "launcher.before"
    old.write_text("old")
    oauth = subject.fastmcp_state
    oauth.mkdir()
    (oauth / "rotated-token").write_text("new-state")
    subject = replace(subject, current_launcher_sha=hashlib.sha256(b"old").hexdigest())

    HostOperations(subject).restore_launcher()

    assert subject.launcher.read_text() == "old"
    assert (oauth / "rotated-token").read_text() == "new-state"


def test_state_upgrade_rechecks_offline_systemd_and_public_gate_before_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    commands: list[list[str]] = []

    monkeypatch.setattr(operations, "_service_state", lambda: "INACTIVE")
    monkeypatch.setattr(operations, "gate_exact", lambda: True)
    monkeypatch.setattr(operations, "public_gated", lambda: True)
    monkeypatch.setattr(operations, "_candidate_control_environment", dict)
    monkeypatch.setattr(
        maintenance,
        "run_host_command",
        lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(
                command, 0,
                "SWITCHSTAND_STATE_UPGRADE_RESULT=NO_EFFECT "
                "revision=0025_task_control_checkpoints\n", "",
            )
        ),
    )

    operations.upgrade_state()

    assert commands == [
        [str(subject.candidate_runtime / "scripts" / "switchstand-upgrade-state"),
         "--target", "production"]
    ]


def test_state_upgrade_nonzero_result_is_unknown_not_a_safe_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    _subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    monkeypatch.setattr(operations, "_service_state", lambda: "INACTIVE")
    monkeypatch.setattr(operations, "gate_exact", lambda: True)
    monkeypatch.setattr(operations, "public_gated", lambda: True)
    monkeypatch.setattr(operations, "_candidate_control_environment", dict)
    monkeypatch.setattr(
        maintenance,
        "run_host_command",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", ""),
    )

    with pytest.raises(Unknown, match="did not complete"):
        operations.upgrade_state()


@pytest.mark.parametrize("output", [
    "SWITCHSTAND_STATE_UPGRADE_RESULT=NO_EFFECT revision=0025",
    (
        "SWITCHSTAND_STATE_UPGRADE_RESULT=NO_EFFECT "
        "revision=0025_task_control_checkpoints trailing=true"
    ),
    (
        "SWITCHSTAND_STATE_UPGRADE_RESULT=APPLIED from=0024_mcp_operation_timings "
        "to=0025_task_control_checkpoints preserved_counts=1|2 backup=relative.dump"
    ),
])
def test_state_upgrade_rejects_truncated_trailing_or_unbound_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str,
):
    _subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    monkeypatch.setattr(operations, "_service_state", lambda: "INACTIVE")
    monkeypatch.setattr(operations, "gate_exact", lambda: True)
    monkeypatch.setattr(operations, "public_gated", lambda: True)
    monkeypatch.setattr(operations, "_candidate_control_environment", dict)
    monkeypatch.setattr(
        maintenance, "run_host_command",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, output + "\n", ""),
    )

    with pytest.raises(Unknown, match="result is not exact"):
        operations.upgrade_state()


def test_state_upgrade_accepts_exact_applied_result_with_private_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    _subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    backup = tmp_path / "state.dump"
    backup.write_bytes(b"backup")
    backup.chmod(0o600)
    monkeypatch.setattr(operations, "_service_state", lambda: "INACTIVE")
    monkeypatch.setattr(operations, "gate_exact", lambda: True)
    monkeypatch.setattr(operations, "public_gated", lambda: True)
    monkeypatch.setattr(operations, "_candidate_control_environment", dict)
    output = (
        "SWITCHSTAND_STATE_UPGRADE_RESULT=APPLIED "
        "from=0024_mcp_operation_timings to=0025_task_control_checkpoints "
        f"preserved_counts=1|2 backup={backup}\n"
    )
    monkeypatch.setattr(
        maintenance, "run_host_command",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, output, ""),
    )

    assert operations.upgrade_state() == "APPLIED"


def test_host_r0_semantic_probe_consumes_current_owner_and_freezes_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    values = {key: f"value-{name}" for name, key in maintenance.R0_ENV.items()}
    tools: list[object] = []
    values[maintenance.R0_ENV["tools_sha256"]] = hashlib.sha256(b"[]").hexdigest()
    subject.env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        receipt_path = Path(command[command.index("--receipt") + 1])
        results = {"product_currentness": {
                "status": "ok",
                "product_work_id": str(maintenance.STATEFUL_PRODUCT_WORK_ID),
                "current": "TRUE", "blockers": [],
            }}
        receipt_path.write_text(json.dumps({
            "schema": 1, "result": "PASS", "candidate_sha": subject.candidate_sha,
            "endpoint": subject.local_url,
            "expected_principal": values[maintenance.R0_ENV["principal"]],
            "tools": tools,
            "tools_sha256": values[maintenance.R0_ENV["tools_sha256"]],
            "work_id": values[maintenance.R0_ENV["work_id"]],
            "foreign_work_id": values[maintenance.R0_ENV["foreign_work_id"]],
            "dependency_work_id": values[maintenance.R0_ENV["dependency_work_id"]],
            "results": results,
            "results_sha256": hashlib.sha256(json.dumps(
                results, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest(),
            "production_mutation": "NOT_RUN", "wrong_token_http_status": 401,
            "transcript": [{"request": {}}],
        }))
        receipt_path.chmod(0o600)
        return subprocess.CompletedProcess(command, 0, "PASS\n", "")

    monkeypatch.setattr(maintenance, "run_host_command", run)

    digest = operations.semantic_ready()

    assert len(digest) == 64
    assert commands[0][0] == str(
        subject.candidate_runtime / "scripts" / "switchstand-edge-semantic-probe"
    )
    assert commands[0][commands[0].index("--endpoint") + 1] == subject.local_url
    assert operations.semantic_ready() == digest
    assert len(commands) == 2


def test_host_r0_missing_semantic_inputs_fails_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    _subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    monkeypatch.setattr(
        maintenance, "run_host_command",
        lambda *_args, **_kwargs: pytest.fail("probe must not run"),
    )

    with pytest.raises(Failed, match="inputs are unavailable"):
        operations.semantic_ready()


def test_host_r0_does_not_reuse_stale_receipt_when_currentness_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    values = {key: f"value-{name}" for name, key in maintenance.R0_ENV.items()}
    tools: list[object] = []
    values[maintenance.R0_ENV["tools_sha256"]] = hashlib.sha256(b"[]").hexdigest()
    subject.env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    stale = subject.attempt_dir / "semantic-probe.json"
    stale.write_text('{"result":"PASS"}\n')
    stale.chmod(0o600)
    calls = 0

    def run(command, **_kwargs):
        nonlocal calls
        calls += 1
        results = {"product_currentness": {
            "status": "ok", "product_work_id": str(maintenance.STATEFUL_PRODUCT_WORK_ID),
            "current": "FALSE", "blockers": ["new-blocker"],
        }}
        receipt_path = Path(command[command.index("--receipt") + 1])
        receipt_path.write_text(json.dumps({
            "schema": 1, "result": "PASS", "candidate_sha": subject.candidate_sha,
            "endpoint": subject.local_url,
            "expected_principal": values[maintenance.R0_ENV["principal"]],
            "tools": tools,
            "tools_sha256": values[maintenance.R0_ENV["tools_sha256"]],
            "work_id": values[maintenance.R0_ENV["work_id"]],
            "foreign_work_id": values[maintenance.R0_ENV["foreign_work_id"]],
            "dependency_work_id": values[maintenance.R0_ENV["dependency_work_id"]],
            "results": results,
            "results_sha256": hashlib.sha256(json.dumps(
                results, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest(),
            "production_mutation": "NOT_RUN", "wrong_token_http_status": 401,
            "transcript": [{"request": {}}],
        }))
        receipt_path.chmod(0o600)
        return subprocess.CompletedProcess(command, 0, "PASS\n", "")

    monkeypatch.setattr(maintenance, "run_host_command", run)

    with pytest.raises(Unknown, match="not current PASS"):
        operations.semantic_ready()
    assert calls == 1
    assert stale.read_text() == '{"result":"PASS"}\n'


def test_no_effect_proof_binds_old_runtime_and_exact_database_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    _subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    commands: list[list[str]] = []
    monkeypatch.setattr(operations, "_resume_trust", lambda: None)
    monkeypatch.setattr(operations, "_gate_state", lambda: "ABSENT")
    monkeypatch.setattr(operations, "_service_state", lambda: "ACTIVE")
    monkeypatch.setattr(operations, "rollback_ready", lambda: True)
    monkeypatch.setattr(operations, "public_ready", lambda: True)

    def run(command, **_kwargs):
        commands.append(command)
        if command[:2] == ["docker", "ps"]:
            output = "database-container\n"
        elif command[:2] == ["docker", "inspect"] and "Mounts" in command[3]:
            output = "switchstand_postgres-data|true\n"
        elif command[:2] == ["docker", "inspect"]:
            output = "switchstand|postgres|postgres:18-alpine|healthy\n"
        elif command[:3] == ["docker", "volume", "inspect"]:
            output = "switchstand|postgres-data\n"
        elif command[:2] == ["docker", "exec"]:
            output = "0024_mcp_operation_timings|ABSENT\n"
        else:
            pytest.fail(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(maintenance, "run_host_command", run)

    operations.prove_upgrade_no_effect()

    assert commands[-1][:2] == ["docker", "exec"]
    assert "task_control_checkpoints" in commands[-1][-1]


@pytest.mark.parametrize(
    "database", [
        "0025_task_control_checkpoints|task_control_checkpoints",
        "0024_mcp_operation_timings|task_control_checkpoints",
        "0023_activation_continuity|ABSENT",
    ],
)
def test_no_effect_proof_rejects_revision_or_table_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: str,
):
    _subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    monkeypatch.setattr(operations, "_resume_trust", lambda: None)
    monkeypatch.setattr(operations, "_gate_state", lambda: "ABSENT")
    monkeypatch.setattr(operations, "_service_state", lambda: "ACTIVE")
    monkeypatch.setattr(operations, "rollback_ready", lambda: True)
    monkeypatch.setattr(operations, "public_ready", lambda: True)

    def run(command, **_kwargs):
        if command[:2] == ["docker", "ps"]:
            output = "database-container\n"
        elif command[:2] == ["docker", "inspect"] and "Mounts" in command[3]:
            output = "switchstand_postgres-data|true\n"
        elif command[:2] == ["docker", "inspect"]:
            output = "switchstand|postgres|postgres:18-alpine|healthy\n"
        elif command[:3] == ["docker", "volume", "inspect"]:
            output = "switchstand|postgres-data\n"
        else:
            output = database + "\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(maintenance, "run_host_command", run)

    with pytest.raises(Unknown, match="exact pre-upgrade state"):
        operations.prove_upgrade_no_effect()


def test_state_upgrade_passes_exact_detached_candidate_selectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    common = tmp_path / "git-common"
    common.mkdir()
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(operations, "_service_state", lambda: "INACTIVE")
    monkeypatch.setattr(operations, "gate_exact", lambda: True)
    monkeypatch.setattr(operations, "public_gated", lambda: True)

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "git":
            if command[-1] == "HEAD":
                output = subject.candidate_sha + "\n"
            elif command[3] == "status":
                output = ""
            elif command[3] == "branch":
                output = ""  # Detached candidate checkout.
            else:
                output = str(common) + "\n"
        else:
            output = (
                "SWITCHSTAND_STATE_UPGRADE_RESULT=NO_EFFECT "
                "revision=0025_task_control_checkpoints\n"
            )
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(maintenance, "run_host_command", run)

    operations.upgrade_state()

    command, kwargs = calls[-1]
    assert command == [
        str(subject.candidate_runtime / "scripts" / "switchstand-upgrade-state"),
        "--target", "production",
    ]
    assert kwargs["env"] is not None
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["SWITCHSTAND_CONTROL_PATH"] == str(subject.candidate_runtime)
    assert environment["SWITCHSTAND_CONTROL_SHA"] == subject.candidate_sha
    assert environment["SWITCHSTAND_CONTROL_COMMON"] == str(common)


def test_state_upgrade_overwrites_inherited_control_selectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    common = tmp_path / "git-common"
    common.mkdir()
    command_env: dict[str, str] | None = None
    monkeypatch.setattr(operations, "_service_state", lambda: "INACTIVE")
    monkeypatch.setattr(operations, "gate_exact", lambda: True)
    monkeypatch.setattr(operations, "public_gated", lambda: True)
    for key in (
        "SWITCHSTAND_CONTROL_PATH",
        "SWITCHSTAND_CONTROL_SHA",
        "SWITCHSTAND_CONTROL_COMMON",
    ):
        monkeypatch.setenv(key, "inherited-wrong-selector")

    def run(command, **kwargs):
        nonlocal command_env
        if command[0] == "git":
            if command[-1] == "HEAD":
                output = subject.candidate_sha + "\n"
            elif command[3] == "status" or command[3] == "branch":
                output = ""
            else:
                output = str(common) + "\n"
        else:
            command_env = kwargs["env"]
            output = (
                "SWITCHSTAND_STATE_UPGRADE_RESULT=NO_EFFECT "
                "revision=0025_task_control_checkpoints\n"
            )
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(maintenance, "run_host_command", run)

    operations.upgrade_state()

    assert command_env is not None
    assert command_env["SWITCHSTAND_CONTROL_PATH"] == str(subject.candidate_runtime)
    assert command_env["SWITCHSTAND_CONTROL_SHA"] == subject.candidate_sha
    assert command_env["SWITCHSTAND_CONTROL_COMMON"] == str(common)


def test_state_upgrade_rejects_candidate_control_mismatch_before_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    _subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    common = tmp_path / "git-common"
    common.mkdir()
    commands: list[list[str]] = []
    monkeypatch.setattr(operations, "_service_state", lambda: "INACTIVE")
    monkeypatch.setattr(operations, "gate_exact", lambda: True)
    monkeypatch.setattr(operations, "public_gated", lambda: True)

    def run(command, **_kwargs):
        commands.append(command)
        if command[0] == "git":
            if command[-1] == "HEAD":
                output = "not-the-candidate\n"
            elif command[3] == "status" or command[3] == "branch":
                output = ""
            else:
                output = str(common) + "\n"
        else:
            pytest.fail("the upgrader must not run after a control mismatch")
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(maintenance, "run_host_command", run)

    with pytest.raises(Unknown, match="control identity is not exact"):
        operations.upgrade_state()

    assert all("switchstand-upgrade-state" not in command[0] for command in commands)


def test_rollback_doctor_uses_candidate_tool_for_old_runtime_without_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    subject, operations, _state = resume_subject(tmp_path, monkeypatch)
    commands: list[list[str]] = []
    assert not (subject.current_runtime / ".venv").exists()
    monkeypatch.setattr(
        maintenance,
        "run_host_command",
        lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    assert operations._doctor(subject.current_runtime, subject.current_sha, False)

    assert commands == [[
        str(subject.candidate_runtime / "scripts" / "switchstand-edge-doctor"),
        "--env-file", str(subject.env_file),
        "--repo", str(subject.current_runtime),
        "--expected-sha", subject.current_sha,
        "--local-url", subject.local_url,
        "--public-url", subject.local_url,
    ]]


def test_launch_mapping_rejects_wrong_runtime_or_oauth_store(tmp_path: Path):
    subject = config(tmp_path)
    subject.launcher.write_text(f"runtime = {subject.current_runtime}\n")
    subject.candidate_launcher.write_text(f"runtime = {subject.current_runtime}\n")
    with pytest.raises(Failed, match="runtime retarget"):
        _validate_launch_mapping(replace(subject, fastmcp_state=FASTMCP_STATE))

    subject.candidate_launcher.write_text(f"runtime = {subject.candidate_runtime}\n")
    with pytest.raises(Failed, match="production target identity"):
        validate_target(replace(subject, fastmcp_state=tmp_path / "wrong-state"))

    _validate_launch_mapping(subject)


def test_launch_mapping_accepts_exact_split_literal_runtime_retarget(tmp_path: Path):
    subject = config(tmp_path)

    def launcher(runtime: Path) -> str:
        return (
            "from pathlib import Path\n"
            "RUNTIME = Path(\n"
            f"    {(str(runtime.parent) + '/')!r}\n"
            f"    {runtime.name!r}\n"
            ")\n"
        )

    subject.launcher.write_text(launcher(subject.current_runtime))
    subject.candidate_launcher.write_text(launcher(subject.candidate_runtime))

    _validate_launch_mapping(subject)


def test_launch_mapping_rejects_non_runtime_edit_with_split_literal(tmp_path: Path):
    subject = config(tmp_path)

    def launcher(runtime: Path) -> str:
        return (
            "from pathlib import Path\n"
            "RUNTIME = Path(\n"
            f"    {(str(runtime.parent) + '/')!r}\n"
            f"    {runtime.name!r}\n"
            ")\n"
        )

    subject.launcher.write_text(launcher(subject.current_runtime))
    subject.candidate_launcher.write_text(
        launcher(subject.candidate_runtime) + "# unrelated edit\n"
    )

    with pytest.raises(Failed, match="exact runtime retarget"):
        _validate_launch_mapping(subject)


def test_launch_mapping_rejects_split_literal_interstitial_comment_edit(tmp_path: Path):
    subject = config(tmp_path)

    def launcher(runtime: Path, comment: str) -> str:
        return (
            "from pathlib import Path\n"
            "RUNTIME = Path(\n"
            f"    {(str(runtime.parent) + '/')!r}\n"
            f"    # {comment}\n"
            f"    {runtime.name!r}\n"
            ")\n"
        )

    subject.launcher.write_text(launcher(subject.current_runtime, "current runtime"))
    subject.candidate_launcher.write_text(
        launcher(subject.candidate_runtime, "candidate runtime")
    )

    with pytest.raises(Failed, match="exact runtime retarget"):
        _validate_launch_mapping(subject)


def test_disposable_target_rejects_every_live_identity_and_escaping_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rehearsals = tmp_path / "rehearsals"
    root = rehearsals / "proof"
    subject = replace(
        config(root), target="disposable", target_root=root,
        service="switchstand-rehearsal-proof.service",
        fastmcp_state=root / "fastmcp",
        caddy="http://127.0.0.1:22019", local_url="http://127.0.0.1:28790/mcp",
        public_origin="http://127.0.0.1:28443", lock_path=root / "edge.lock",
    )
    rehearsals.chmod(0o700)
    root.chmod(0o700)
    monkeypatch.setattr(maintenance, "REHEARSALS", rehearsals)
    validate_target(subject)
    live = {
        "service": maintenance.SERVICE, "fastmcp_state": FASTMCP_STATE,
        "caddy": maintenance.CADDY, "local_url": maintenance.LOCAL_URL,
        "public_origin": maintenance.PUBLIC_ORIGIN, "lock_path": maintenance.LOCK,
    }
    for field, value in live.items():
        with pytest.raises(Failed):
            validate_target(replace(subject, **{field: value}))  # pyright: ignore[reportCallIssue]
    with pytest.raises(Failed):
        validate_target(replace(subject, local_url="http://127.0.0.1:8790/other"))
    with pytest.raises(Failed):
        validate_target(replace(subject, target="production"))

    outside = tmp_path / "outside"
    escaped = replace(subject, attempt_dir=outside)
    assert deploy(escaped, HostOperations(escaped)) == "FAIL"
    assert not outside.exists()


def test_disposable_target_rejects_symlinked_rehearsal_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    production = tmp_path / "production"
    production.mkdir(mode=0o700)
    rehearsals = tmp_path / "rehearsals"
    rehearsals.symlink_to(production, target_is_directory=True)
    root = rehearsals / "proof"
    subject = replace(
        config(root), target="disposable", target_root=root,
        service="switchstand-rehearsal-proof.service",
        fastmcp_state=root / "fastmcp",
        caddy="http://127.0.0.1:22019", local_url="http://127.0.0.1:28790/mcp",
        public_origin="http://127.0.0.1:28443", lock_path=root / "edge.lock",
    )
    monkeypatch.setattr(maintenance, "REHEARSALS", rehearsals)
    with pytest.raises(Failed, match="root is not exact"):
        validate_target(subject)


def test_cli_rejects_target_identity_before_attempt_directory(tmp_path: Path):
    attempt = tmp_path / "outside-attempt"
    arguments = [
        "--attempt-dir", str(attempt),
        "--current-runtime", str(tmp_path / "current"),
        "--candidate-runtime", str(tmp_path / "candidate"),
        "--candidate-launcher", str(tmp_path / "candidate-launcher"),
        "--launcher", str(tmp_path / "launcher"),
        "--fastmcp-state", str(tmp_path / "not-production-state"),
        "--env-file", str(tmp_path / "edge.env"),
        "--current-sha", "a" * 40,
        "--candidate-sha", "b" * 40,
        "--candidate-launcher-sha", "c" * 64,
        "--current-launcher-sha", "d" * 64,
    ]
    with pytest.raises(Failed, match="production target identity"):
        maintenance.main(arguments)
    assert not attempt.exists()


def test_disposable_service_command_cannot_select_production_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    subject = replace(config(tmp_path), service="switchstand-rehearsal-proof.service")
    commands: list[list[str]] = []

    def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="inactive\n", stderr="")

    monkeypatch.setattr(maintenance, "run_host_command", run)
    HostOperations(subject)._service("stop", "inactive")  # pyright: ignore[reportPrivateUsage]
    assert all(subject.service in command for command in commands)
    assert all(maintenance.SERVICE not in command for command in commands)


def test_running_process_binds_launcher_and_fastmcp_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pid = os.getpid()
    arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    launcher = Path(os.fsdecode(next(argument for argument in arguments if argument)))
    data_home = os.environ.get("XDG_DATA_HOME")
    expected_state = Path(
        os.environ.get("FASTMCP_HOME")
        or ((Path(data_home) if data_home else Path.home() / ".local/share") / "fastmcp")
    )
    subject = replace(config(tmp_path), launcher=launcher, fastmcp_state=expected_state)

    def main_pid(_command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout=f"{pid}\n", stderr="")

    monkeypatch.setattr(maintenance, "run_host_command", main_pid)
    operations = HostOperations(subject)
    assert operations._running_process_exact()
    assert not HostOperations(
        replace(subject, launcher=tmp_path / "wrong")
    )._running_process_exact()
    assert not HostOperations(
        replace(subject, fastmcp_state=tmp_path / "wrong-state")
    )._running_process_exact()


def test_service_lock_contends_across_different_attempt_parents(tmp_path: Path):
    first = config(tmp_path / "first")
    second = config(tmp_path / "second")
    assert first.attempt_dir.parent != second.attempt_dir.parent
    lock = tmp_path / "service.lock"

    with (
        exclusive_lock(lock),
        pytest.raises(SystemExit, match="another edge maintenance attempt"),
    ):
        exclusive_lock(lock)


def test_gate_does_not_suppress_operator_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    operations = HostOperations(config(tmp_path))

    def interrupting_api(_method: str, _path: str, _body: object | None = None) -> object:
        raise Interrupted("stop")

    monkeypatch.setattr(operations, "_api", interrupting_api)
    with pytest.raises(Interrupted):
        operations.gate()


def test_ungate_does_not_suppress_operator_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    operations = HostOperations(config(tmp_path))

    def interrupting_api(_method: str, _path: str, _body: object | None = None) -> object:
        raise Interrupted("stop")

    monkeypatch.setattr(operations, "_api", interrupting_api)
    with pytest.raises(Interrupted):
        operations.ungate()


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_real_caddy_gate_covers_mcp_oauth_and_metadata_paths(tmp_path: Path):
    caddy = shutil.which("caddy")
    if caddy is None:
        pytest.skip("Caddy is not installed")
    admin_port, public_port = _unused_port(), _unused_port()
    caddy_config = tmp_path / "caddy.json"
    caddy_config.write_text(
        json.dumps(
            {
                "admin": {"listen": f"127.0.0.1:{admin_port}"},
                "apps": {
                    "http": {
                        "servers": {
                            "dish_action_router": {
                                "listen": [f"127.0.0.1:{public_port}"],
                                "routes": [
                                    {
                                        "handle": [
                                            {"handler": "static_response", "status_code": "418"}
                                        ]
                                    }
                                ],
                            }
                        }
                    }
                },
            }
        )
    )
    process = subprocess.Popen(
        [caddy, "run", "--config", str(caddy_config)],
        env={
            "HOME": str(tmp_path),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{admin_port}/config/", timeout=0.1)
                break
            except OSError:
                time.sleep(0.02)
        subject = config(
            tmp_path / "subject",
            caddy=f"http://127.0.0.1:{admin_port}",
            public_origin=f"http://127.0.0.1:{public_port}",
        )
        operations = HostOperations(subject)

        operations.gate()
        assert operations.gate_exact()
        assert operations.public_gated()
        for path in (
            "/switchstand/mcp",
            "/switchstand/mcp/maintenance-probe",
            "/.well-known/switchstand-certification-runtime",
        ):
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"http://127.0.0.1:{public_port}{path}", timeout=1)
            assert exc.value.code == 503
            assert exc.value.headers["Retry-After"] == "60"
        durable = maintenance.Receipt(subject)
        durable.write("GATED")

        class RealCaddyAbort:
            def gate_abort_ready(self):
                return True

            def ungate(self):
                operations.ungate()

            def gate_exact(self):
                return operations.gate_exact()

            def public_ready(self):
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{public_port}/switchstand/mcp", timeout=1
                    )
                except urllib.error.HTTPError as exc:
                    return exc.code == 418
                return False

        assert maintenance.abort_unproved_gate(durable, RealCaddyAbort()) == "FAIL"
        assert not operations.gate_exact()
        assert receipt(subject)["phase"] == "ROLLED_BACK"
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(
                f"http://127.0.0.1:{public_port}/switchstand/mcp", timeout=1
            )
        assert exc.value.code == 418
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_https_gate_proof_uses_public_dns_pinning(monkeypatch, tmp_path: Path):
    subject = config(tmp_path, public_origin="https://edge.example")
    operations = HostOperations(subject)
    requests = []

    monkeypatch.setattr(
        maintenance.ExternalIngressHttp,
        "public_addresses",
        lambda _self, host: ("8.8.8.8",) if host == "edge.example" else (),
    )

    def request(host, address, method, path):
        requests.append((host, address, method, path))
        return 503, {"retry-after": "60"}, b""

    monkeypatch.setattr(
        maintenance.ExternalIngressHttp, "request", staticmethod(request)
    )
    monkeypatch.setattr(
        maintenance.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("normal host DNS must not prove Funnel"),
    )

    assert operations.public_gated()
    assert len(requests) == len(maintenance.EDGE_PATHS)
    assert all(call[:3] == ("edge.example", "8.8.8.8", "GET") for call in requests)


def test_https_gate_proof_fails_when_one_public_address_times_out(
    monkeypatch, tmp_path: Path
):
    subject = config(tmp_path, public_origin="https://edge.example")
    operations = HostOperations(subject)
    requests = []

    monkeypatch.setattr(
        maintenance.ExternalIngressHttp,
        "public_addresses",
        lambda _self, host: ("8.8.8.8", "9.9.9.9") if host == "edge.example" else (),
    )

    def request(host, address, method, path):
        requests.append((host, address, method, path))
        if address == "9.9.9.9":
            raise TimeoutError
        return 503, {"retry-after": "60"}, b""

    monkeypatch.setattr(
        maintenance.ExternalIngressHttp, "request", staticmethod(request)
    )

    assert not operations.public_gated()
    assert any(call[1] == "8.8.8.8" for call in requests)
    assert any(call[1] == "9.9.9.9" for call in requests)


def test_public_readiness_requires_external_ingress_probe(monkeypatch, tmp_path: Path):
    subject = config(tmp_path, public_origin="https://edge.example")
    operations = HostOperations(subject)
    monkeypatch.setattr(maintenance, "_sha", lambda _path: subject.candidate_launcher_sha)
    monkeypatch.setattr(operations, "_doctor", lambda *_args: True)

    monkeypatch.setattr(
        maintenance.ExternalIngressHttp,
        "observe",
        lambda _self: HttpObservation(False, None),
    )
    assert not operations.public_ready()

    monkeypatch.setattr(
        maintenance.ExternalIngressHttp,
        "observe",
        lambda _self: HttpObservation(True, 401, valid_auth_challenge=True),
    )
    assert operations.public_ready()


def test_current_public_readiness_binds_old_runtime(monkeypatch, tmp_path: Path):
    subject = config(tmp_path, public_origin="https://edge.example")
    operations = HostOperations(subject)
    calls = []
    monkeypatch.setattr(
        maintenance.ExternalIngressHttp,
        "observe",
        lambda _self: HttpObservation(True, 401, valid_auth_challenge=True),
    )
    monkeypatch.setattr(
        operations,
        "_doctor",
        lambda runtime, expected, public: calls.append((runtime, expected, public)) or True,
    )

    assert operations.current_public_ready()
    assert calls == [(subject.current_runtime, subject.current_sha, True)]
