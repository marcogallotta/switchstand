import hashlib
import json
from pathlib import Path
from typing import Literal

import pytest

from switchstand.edge_maintenance import Failed, Unknown
from switchstand.stage12_cutover import (
    Evidence,
    FrozenEvidence,
    Reconciled,
    Stage12Cutover,
)

SHA = "a" * 40
DIGEST = "b" * 64


def _private(path: Path, data: bytes = b"receipt") -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _subject(tmp_path: Path) -> tuple[Stage12Cutover, Commands]:
    manifest: dict[str, object] = {
        "schema_version": 1, "source_candidate": SHA, "rows": [], "exceptions": [],
        "counts": {"broad": 0, "bound": 0, "included": 0, "exceptions": 0},
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["sha256"] = hashlib.sha256(canonical).hexdigest()
    encoded = json.dumps(manifest, sort_keys=True).encode() + b"\n"
    a, b, worksheet = tmp_path / "a", tmp_path / "b", tmp_path / "worksheet"
    _private(a, encoded)
    _private(b, encoded)
    _private(worksheet, b"worksheet")
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    evidence = Evidence(
        SHA, (a, b), str(manifest["sha256"]), hashlib.sha256(b"[]").hexdigest(),
        worksheet, hashlib.sha256(b"worksheet").hexdigest(), 1,
    )
    commands = Commands()
    return Stage12Cutover(attempt, evidence, commands), commands


class Commands:
    def __init__(self):
        self.schema: Literal["ABSENT", "APPLIED", "UNKNOWN"] = "ABSENT"
        self.stage1: Literal["ABSENT", "APPLIED", "UNKNOWN"] = "ABSENT"
        self.stage2: Literal["ABSENT", "APPLIED", "UNKNOWN"] = "ABSENT"
        self.calls: list[str] = []
        self.crash: str | None = None
        self.validation = self.prepared = DIGEST
        self.backup = "c" * 64

    def _state(self, value: Literal["ABSENT", "APPLIED", "UNKNOWN"], name: str) -> Reconciled:
        return Reconciled(value, DIGEST if value == "APPLIED" else "",
            self.backup if name == "schema" and value == "APPLIED" else "",
        )

    def schema_state(self, receipt: Path) -> Reconciled:
        self.calls.append("schema-state")
        return self._state(self.schema, "schema")

    def apply_schema(self, receipt: Path) -> None:
        self.calls.append("schema-apply")
        _private(receipt)
        self.schema = "APPLIED"
        if self.crash == "schema":
            raise Unknown("crash")

    def abort_schema(self, receipt: Path) -> None:
        self.calls.append("schema-abort")
        self.schema = "ABSENT"

    def validate_stage2_pre_authority(self, evidence: FrozenEvidence) -> str:
        self.calls.append("stage2-validate")
        if self.crash == "validate":
            raise Failed("invalid worksheet")
        return self.validation

    def prepare_stage1(self, evidence: FrozenEvidence) -> str:
        self.calls.append("stage1-prepare")
        return self.prepared

    def capture_final_corpus(self, evidence: FrozenEvidence, destination: Path) -> str:
        self.calls.append("capture-final")
        _private(destination, evidence.manifests[0].read_bytes())
        return evidence.corpus_digest

    def cleanup_stage1(self, evidence: FrozenEvidence) -> None:
        self.calls.append("stage1-cleanup")

    def stage1_state(self, receipt: Path) -> Reconciled:
        self.calls.append("stage1-state")
        return self._state(self.stage1, "stage1")

    def activate_stage1(self, prepared_digest: str, receipt: Path) -> None:
        self.calls.append("stage1-activate")
        _private(receipt)
        self.stage1 = "APPLIED"
        if self.crash == "stage1":
            raise Unknown("crash")

    def stage2_state(self, receipt: Path) -> Reconciled:
        self.calls.append("stage2-state")
        return self._state(self.stage2, "stage2")

    def activate_stage2(self, receipt: Path) -> None:
        self.calls.append("stage2-activate")
        _private(receipt)
        self.stage2 = "APPLIED"
        if self.crash == "stage2":
            raise Unknown("crash")


def test_invalid_worksheet_never_calls_stage1_and_can_abort_schema(tmp_path: Path):
    subject, commands = _subject(tmp_path)
    commands.crash = "validate"

    with pytest.raises(Failed, match="invalid worksheet"):
        subject.run(lambda _boundary: None)
    subject.abort_pre_authority()

    assert "stage1-activate" not in commands.calls
    assert commands.schema == "ABSENT"


def test_resume_from_durable_preproof_does_not_recompute_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    subject, commands = _subject(tmp_path)
    original = subject._write  # pyright: ignore[reportPrivateUsage]
    pre_writes = 0

    def crash_after_preproof(frozen: FrozenEvidence, value: str, **proof: str):
        nonlocal pre_writes
        result = original(frozen, value, **proof)
        if value == "PRE_MARKER":
            pre_writes += 1
            if pre_writes == 2:
                raise Unknown("crash after preproof")
        return result

    monkeypatch.setattr(subject, "_write", crash_after_preproof)
    with pytest.raises(Unknown):
        subject.run(lambda _boundary: None)
    monkeypatch.setattr(subject, "_write", original)

    subject.run(lambda _boundary: None)

    assert [commands.calls.count(name) for name in (
        "stage2-validate", "stage1-prepare", "capture-final"
    )] == [1, 1, 1]


def test_existing_unapplied_subordinate_receipt_forbids_blind_retry(tmp_path: Path):
    subject, commands = _subject(tmp_path)
    _private(subject._stage1)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(Unknown, match="Stage 1 authority"):
        subject.run(lambda _boundary: None)
    subject.abort_pre_authority()

    assert "stage1-activate" not in commands.calls
    assert commands.schema == "ABSENT"


@pytest.mark.parametrize("proof", ["validation", "prepared", "backup"])
def test_invalid_proof_forbids_authority_effect(tmp_path: Path, proof: str):
    subject, commands = _subject(tmp_path)
    setattr(commands, proof, "malformed" if proof == "backup" else "")

    with pytest.raises((Failed, Unknown)):
        subject.run(lambda _boundary: None)
    assert "stage1-activate" not in commands.calls


@pytest.mark.parametrize("boundary", ["PRE_MARKER", "POSTGRES_AUTHORITY", "COMPLETE"])
def test_resume_after_durable_top_receipt_replays_no_effect(
    tmp_path: Path, boundary: str, monkeypatch: pytest.MonkeyPatch
):
    subject, commands = _subject(tmp_path)
    original = subject._write  # pyright: ignore[reportPrivateUsage]

    def crash_after_write(frozen: FrozenEvidence, value: str, **proof: str):
        result = original(frozen, value, **proof)
        if value == boundary:
            raise Unknown("crash after top receipt")
        return result

    monkeypatch.setattr(subject, "_write", crash_after_write)
    with pytest.raises(Unknown):
        subject.run(lambda _boundary: None)
    effects = [call for call in commands.calls if call.endswith(("-apply", "-activate"))]
    completed = {effect: effects.count(effect) for effect in effects}
    monkeypatch.setattr(subject, "_write", original)

    subject.run(lambda _boundary: None)

    replayed = [call for call in commands.calls if call.endswith(("-apply", "-activate"))]
    assert all(replayed.count(effect) == count for effect, count in completed.items())


@pytest.mark.parametrize(
    ("boundary", "field"),
    [("POSTGRES_AUTHORITY", "stage1_receipt"), ("COMPLETE", "stage2_receipt"),
     ("COMPLETE", "schema_version")],
)
def test_corrupt_advanced_receipt_is_unknown_before_effect(
    tmp_path: Path, boundary: str, field: str
):
    subject, _commands = _subject(tmp_path)
    subject.run(lambda _boundary: None)
    raw = json.loads(subject.receipt_path.read_text())
    raw["terminal_boundary"] = boundary
    raw[field] = "bad"
    _private(subject.receipt_path, json.dumps(raw).encode())

    with pytest.raises(Unknown, match="malformed"):
        subject.run(lambda _boundary: None)


@pytest.mark.parametrize("boundary", ["POSTGRES_AUTHORITY", "COMPLETE"])
def test_advanced_top_receipt_absolutely_forbids_abort(tmp_path: Path, boundary: str):
    subject, commands = _subject(tmp_path)
    subject.run(lambda _boundary: None)
    raw = json.loads(subject.receipt_path.read_text())
    raw["terminal_boundary"] = boundary
    _private(subject.receipt_path, json.dumps(raw).encode())
    commands.stage1 = "ABSENT"
    subject._stage1.unlink()  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(Unknown, match="boundary crossed"):
        subject.abort_pre_authority()
    assert commands.schema == "APPLIED"
