"""Hermetic failure matrix for the composed Stage 1+2 maintenance attempt.

The fakes below model causal state, not command ordering: a fault can happen before
or after one state transition, and reconciliation observes the resulting state.
No Docker, service manager, Caddy, provider, or network boundary is touched.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest

from switchstand.edge_maintenance import (
    CADDY,
    FASTMCP_STATE,
    LOCAL_URL,
    LOCK,
    PUBLIC_ORIGIN,
    SERVICE,
    Config,
    Failed,
    Receipt,
    Unknown,
    deploy,
)
from switchstand.stage12_cutover import Evidence, FrozenEvidence, Reconciled, Stage12Cutover

SHA = "a" * 40
DIGEST = "b" * 64
INTERRUPTION_POINTS = (
    "gate", "stop", "snapshot", "schema", "stage1_prepare", "stage1_commit",
    "stage2_commit", "swap", "start", "probe", "ungate",
)


class Fault:
    def __init__(
        self, point: str | None = None, when: str = "before",
        error: type[RuntimeError] = Unknown,
    ):
        self.point, self.when, self.error, self.used = point, when, error, False

    def before(self, point: str) -> None:
        if self.point == point and self.when == "before" and not self.used:
            self.used = True
            raise self.error(f"interrupted before {point}")

    def after(self, point: str) -> None:
        if self.point == point and self.when == "after" and not self.used:
            self.used = True
            raise self.error(f"interrupted after {point}")


class Host:
    def __init__(self, fault: Fault):
        self.fault = fault
        self.gated = self.snapshot_present = self.swapped = self.probed = False
        self.active = True
        self.transitions: Counter[str] = Counter()

    def _change(self, point: str, change: Callable[[], bool]) -> None:
        self.fault.before(point)
        if change():
            self.transitions[point] += 1
        self.fault.after(point)

    def preflight(self) -> None: pass
    def gate(self) -> None:
        self._change("gate", lambda: False if self.gated else self._set("gated", True))
    def gate_exact(self) -> bool: return self.gated
    def public_gated(self) -> bool: return self.gated
    def stop(self) -> None:
        self._change("stop", lambda: False if not self.active else self._set("active", False))
    def snapshot(self) -> None:
        self._change(
            "snapshot",
            lambda: False if self.snapshot_present else self._set("snapshot_present", True),
        )
    def snapshot_digest(self) -> str:
        if not self.snapshot_present:
            raise Unknown("snapshot absent")
        return "c" * 64
    def swap(self) -> None:
        self._change("swap", lambda: False if self.swapped else self._set("swapped", True))
    def start(self) -> None:
        self._change("start", lambda: False if self.active else self._set("active", True))
    def local_ready(self) -> bool:
        self.fault.before("probe")
        self.probed = True
        self.fault.after("probe")
        return self.active and self.swapped
    def rollback_ready(self) -> bool: return self.active and not self.swapped
    def ungate(self) -> None:
        self._change("ungate", lambda: False if not self.gated else self._set("gated", False))
    def public_ready(self) -> bool: return self.active and not self.gated
    def restore_launcher(self) -> None: self.swapped = False

    def _set(self, name: str, value: bool) -> bool:
        setattr(self, name, value)
        return True

    def reconcile_phase(
        self, phase: str, proof: dict[str, object], *, offline: bool,
    ) -> tuple[str, dict[str, str]]:
        del proof, offline
        if phase == "PREFLIGHT" and self.gated:
            return "GATED", {}
        if phase == "GATED" and not self.active:
            return "STOPPED", {}
        if phase == "STOPPED" and self.snapshot_present:
            return "SNAPSHOTTED", {"fastmcp_snapshot": "c" * 64}
        if phase in {"SNAPSHOTTED", "OFFLINE_COMPLETE"} and self.swapped:
            return "SWAPPED", {}
        if phase == "SWAPPED" and self.active:
            return "STARTED", {}
        if phase == "STARTED" and not self.gated:
            return "UNGATED", {}
        return phase, {}


class Commands:
    def __init__(self, fault: Fault):
        self.fault = fault
        self.schema = self.stage1 = self.stage2 = False
        self.prepared = False
        self.transitions: Counter[str] = Counter()

    def _effect(self, point: str, name: str, receipt: Path | None = None) -> None:
        self.fault.before(point)
        if not getattr(self, name):
            setattr(self, name, True)
            self.transitions[point] += 1
            if receipt is not None:
                _private(receipt)
        self.fault.after(point)

    def schema_state(self, receipt: Path) -> Reconciled:
        if self.schema:
            return Reconciled("APPLIED", DIGEST, "d" * 64) if receipt.exists() else Reconciled("UNKNOWN")
        return Reconciled("ABSENT")
    def apply_schema(self, receipt: Path) -> None:
        self._effect("schema", "schema", receipt)
    def abort_schema(self, receipt: Path) -> None:
        del receipt
        if self.schema:
            self.schema = False
            self.transitions["schema_abort"] += 1
    def validate_stage2_pre_authority(self, evidence: FrozenEvidence) -> str:
        del evidence
        return DIGEST
    def prepare_stage1(self, evidence: FrozenEvidence) -> str:
        del evidence
        self._effect("stage1_prepare", "prepared")
        return DIGEST
    def capture_final_corpus(self, evidence: FrozenEvidence, destination: Path) -> str:
        _private(destination, evidence.manifests[0].read_bytes())
        return evidence.corpus_digest
    def cleanup_stage1(self, evidence: FrozenEvidence) -> None:
        del evidence
        self.prepared = False
        self.transitions["stage1_cleanup"] += 1
    def stage1_state(self, receipt: Path) -> Reconciled:
        if self.stage1:
            return Reconciled("APPLIED", DIGEST) if receipt.exists() else Reconciled("UNKNOWN")
        return Reconciled("ABSENT")
    def activate_stage1(self, prepared_digest: str, receipt: Path) -> None:
        del prepared_digest
        self._effect("stage1_commit", "stage1", receipt)
    def stage2_state(self, receipt: Path) -> Reconciled:
        if self.stage2:
            return Reconciled("APPLIED", DIGEST) if receipt.exists() else Reconciled("UNKNOWN")
        return Reconciled("ABSENT")
    def activate_stage2(self, receipt: Path) -> None:
        self._effect("stage2_commit", "stage2", receipt)


def _private(path: Path, data: bytes = b"receipt\n") -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _subject(
    tmp_path: Path, fault: Fault,
) -> tuple[Config, Host, Commands, Stage12Cutover]:
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    manifest: dict[str, object] = {
        "schema_version": 1, "source_candidate": SHA, "rows": [], "exceptions": [],
        "counts": {"broad": 0, "bound": 0, "included": 0, "exceptions": 0},
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["sha256"] = hashlib.sha256(canonical).hexdigest()
    encoded = json.dumps(manifest, sort_keys=True).encode() + b"\n"
    first, second, worksheet = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "w.json"
    for path, data in ((first, encoded), (second, encoded), (worksheet, b"worksheet\n")):
        _private(path, data)
    evidence = Evidence(
        SHA, (first, second), str(manifest["sha256"]), hashlib.sha256(b"[]").hexdigest(),
        worksheet, hashlib.sha256(worksheet.read_bytes()).hexdigest(), 1,
    )
    config = Config(
        attempt, tmp_path / "old", SHA, tmp_path / "new", SHA,
        tmp_path / "candidate-launcher", "e" * 64, tmp_path / "launcher", "f" * 64,
        FASTMCP_STATE, tmp_path / "edge.env", "production", caddy=CADDY,
        public_origin=PUBLIC_ORIGIN, service=SERVICE, local_url=LOCAL_URL, lock_path=LOCK,
    )
    host, commands = Host(fault), Commands(fault)
    return config, host, commands, Stage12Cutover(attempt, evidence, commands)


@pytest.mark.parametrize("point", INTERRUPTION_POINTS)
@pytest.mark.parametrize("when", ("before", "after"))
def test_interruption_retains_gate_then_same_attempt_resumes_without_duplicate_effect(
    tmp_path: Path, point: str, when: str,
):
    config, host, commands, offline = _subject(tmp_path, Fault(point, when))

    assert deploy(config, host, offline) == "UNKNOWN"
    assert host.gated
    assert Receipt(config, offline).value["status"] == "UNKNOWN"

    assert deploy(config, host, offline) == "PASS"
    assert not host.gated
    for effect in ("schema", "stage1_prepare", "stage1_commit", "stage2_commit"):
        assert commands.transitions[effect] == 1
    for effect in ("snapshot", "swap"):
        assert host.transitions[effect] == 1


def test_receipt_proven_pre_marker_failure_cleans_up_and_recovers_old_runtime(tmp_path: Path):
    config, host, commands, offline = _subject(
        tmp_path, Fault("stage1_prepare", "before", Failed)
    )

    assert deploy(config, host, offline) == "FAIL"
    assert commands.transitions["stage1_cleanup"] == 1
    assert commands.transitions["schema_abort"] == 1
    assert not commands.schema and not commands.stage1
    assert host.active and not host.gated and not host.swapped


def test_post_marker_failure_is_forward_only_and_retains_gate(tmp_path: Path):
    config, host, commands, offline = _subject(tmp_path, Fault("swap", "before", Failed))

    assert deploy(config, host, offline) == "UNKNOWN"
    receipt = Receipt(config, offline).value
    assert receipt["authority_crossed"] is True
    assert receipt["error"] == "ForwardFixRequired"
    assert commands.stage1 and commands.stage2 and commands.transitions["schema_abort"] == 0
    assert host.gated and not host.active


@pytest.mark.parametrize("damage", ("missing", "malformed", "changed"))
def test_unreconcilable_post_marker_receipt_is_unknown_without_replaying_effects(
    tmp_path: Path, damage: Literal["missing", "malformed", "changed"],
):
    config, host, commands, offline = _subject(
        tmp_path, Fault("stage2_commit", "after")
    )
    assert deploy(config, host, offline) == "UNKNOWN"
    before = commands.transitions.copy()
    if damage == "missing":
        offline.receipt_path.unlink()
    elif damage == "malformed":
        _private(offline.receipt_path, b"{")
    else:
        value = json.loads(offline.receipt_path.read_text())
        value["schema_receipt"] = "0" * 64
        _private(offline.receipt_path, json.dumps(value).encode())

    assert deploy(config, host, offline) == "UNKNOWN"
    assert host.gated
    assert commands.transitions == before


def test_ambiguous_caddy_readback_stops_before_service_effects(tmp_path: Path):
    config, host, _commands, offline = _subject(tmp_path, Fault("gate", "after"))
    host.gate_exact = lambda: (_ for _ in ()).throw(Unknown("ambiguous Caddy"))  # type: ignore[method-assign]

    assert deploy(config, host, offline) == "UNKNOWN"
    assert host.transitions["stop"] == 0
    assert Receipt(config, offline).value["error"] == "GateRetentionUnknown"


@pytest.mark.skip(reason="NOT_RUN: real Docker/service/Caddy/provider boundary requires an authorized disposable rehearsal")
def test_real_boundary_failure_matrix_not_run() -> None: ...


@pytest.mark.skip(reason="NOT_RUN: mutation and restart probe matrix is deferred until reviewed C2d2b lands")
def test_c2d2b_probe_failure_matrix_not_run() -> None: ...
