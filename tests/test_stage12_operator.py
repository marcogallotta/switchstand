from pathlib import Path

import pytest

from switchstand.edge_maintenance import (
    CADDY,
    FASTMCP_STATE,
    LOCAL_URL,
    LOCK,
    PUBLIC_ORIGIN,
    SERVICE,
    Config,
    Receipt,
    Unknown,
)
from switchstand.stage12_cutover import Evidence, Stage12Cutover
from switchstand.stage12_operator import abort_window, prepare_window, resume_window, status_window

SHA = "a" * 40
DIGEST = "b" * 64


def _config(tmp_path: Path) -> Config:
    return Config(
        tmp_path / "attempt", tmp_path / "current", SHA, tmp_path / "candidate", SHA,
        tmp_path / "candidate-launcher", "c" * 64, tmp_path / "launcher", "d" * 64,
        FASTMCP_STATE, tmp_path / "edge.env", "production", caddy=CADDY,
        public_origin=PUBLIC_ORIGIN, service=SERVICE, local_url=LOCAL_URL, lock_path=LOCK,
    )


class Review:
    def __init__(self):
        self.begins = self.prepares = self.aborts = 0
        self.digest = DIGEST

    def begin(self) -> None:
        self.begins += 1

    def prepare(self) -> str:
        self.prepares += 1
        return self.digest

    def abort(self) -> None:
        self.aborts += 1

    def status(self) -> str:
        return "REVIEW_PENDING"


class Operations:
    def __init__(self):
        self.calls: list[str] = []
        self.gated = False
        self.active = True
        self.fail_snapshot = False

    def preflight(self): self.calls.append("preflight")
    def gate(self): self.calls.append("gate"); self.gated = True
    def gate_exact(self): return self.gated
    def public_gated(self): return self.gated
    def stop(self): self.calls.append("stop"); self.active = False
    def snapshot(self):
        self.calls.append("snapshot")
        if self.fail_snapshot:
            raise Unknown("snapshot lost")
    def snapshot_digest(self): return "e" * 64
    def start(self): self.calls.append("start"); self.active = True
    def rollback_ready(self): return self.active
    def ungate(self): self.calls.append("ungate"); self.gated = False
    def public_ready(self): return self.active and not self.gated
    def reconcile_phase(self, phase, proof, *, offline): return phase, {}
    def swap(self): raise AssertionError
    def local_ready(self): return False
    def restore_launcher(self): raise AssertionError


def test_prepare_stops_at_review_pending_and_reentry_replays_no_host_effect(tmp_path: Path):
    config, operations, review = _config(tmp_path), Operations(), Review()
    config.attempt_dir.mkdir(mode=0o700)
    assert prepare_window(config, operations, review) == "REVIEW_PENDING"
    first = list(operations.calls)
    assert prepare_window(config, operations, review) == "REVIEW_PENDING"
    receipt = Receipt(config, None)
    assert receipt.value["phase"] == "SNAPSHOTTED"
    assert receipt.value["status"] == "RUNNING"
    assert status_window(config, review) == "REVIEW_PENDING"
    assert operations.calls == first
    assert review.begins == 2 and review.prepares == 2


def test_prepare_ambiguity_retains_gate_and_records_unknown(tmp_path: Path):
    config, operations, review = _config(tmp_path), Operations(), Review()
    config.attempt_dir.mkdir(mode=0o700)
    operations.fail_snapshot = True
    assert prepare_window(config, operations, review) == "UNKNOWN"
    receipt = Receipt(config, None)
    assert operations.gated
    assert receipt.value["status"] == "UNKNOWN"
    assert receipt.value["phase"] == "STOPPED"
    assert review.prepares == 0


def test_abort_cleans_review_then_recovers_old_runtime(tmp_path: Path):
    config, operations, review = _config(tmp_path), Operations(), Review()
    config.attempt_dir.mkdir(mode=0o700)
    assert prepare_window(config, operations, review) == "REVIEW_PENDING"
    assert abort_window(config, operations, review) == "FAIL"
    receipt = Receipt(config, None)
    assert receipt.value["phase"] == "ROLLED_BACK"
    assert receipt.value["status"] == "FAIL"
    assert review.aborts == 1
    assert operations.calls[-2:] == ["start", "ungate"]
    before = list(operations.calls)
    assert prepare_window(config, operations, review) == "UNKNOWN"
    assert operations.calls == before


def test_resume_adopts_exact_approved_worksheet_before_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    config, operations, review = _config(tmp_path), Operations(), Review()
    config.attempt_dir.mkdir(mode=0o700)
    assert prepare_window(config, operations, review) == "REVIEW_PENDING"
    manifests = (tmp_path / "a.json", tmp_path / "b.json")
    worksheet = tmp_path / "worksheet.json"
    for path in (*manifests, worksheet):
        path.write_bytes(b"evidence")
        path.chmod(0o600)
    evidence = Evidence(SHA, manifests, DIGEST, "f" * 64, worksheet, DIGEST, 1)
    deployed: list[Stage12Cutover] = []

    def deploy(_config, _operations, offline):
        deployed.append(offline)
        return "PASS"

    monkeypatch.setattr("switchstand.stage12_operator.deploy", deploy)

    assert resume_window(config, operations, review, evidence, DIGEST) == "PASS"
    assert len(deployed) == 1
    receipt = Receipt(config, deployed[0])
    assert receipt.value["offline_worksheet"] == DIGEST


def test_resume_rejects_unapproved_digest_before_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    config, operations, review = _config(tmp_path), Operations(), Review()
    config.attempt_dir.mkdir(mode=0o700)
    assert prepare_window(config, operations, review) == "REVIEW_PENDING"
    path = tmp_path / "evidence"
    path.write_bytes(b"evidence")
    evidence = Evidence(SHA, (path, path), DIGEST, DIGEST, path, "c" * 64, 1)
    called = False

    def deploy(*_args):
        nonlocal called
        called = True
        return "PASS"

    monkeypatch.setattr("switchstand.stage12_operator.deploy", deploy)
    assert resume_window(config, operations, review, evidence, "c" * 64) == "UNKNOWN"
    assert not called
    assert operations.gated
    assert Receipt(config, None).value["offline_receipt"] is None
