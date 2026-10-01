"""Explicit two-step host operator for the Stage 1+2 review checkpoint."""

from __future__ import annotations

import subprocess
from typing import cast

from .edge_maintenance import (
    Config,
    Failed,
    Operations,
    Receipt,
    Unknown,
    deploy,
    retain_gate,
    validate_target,
)
from .stage12_cutover import (
    ConcreteCommands,
    Evidence,
    ReviewCheckpoint,
    Stage12Cutover,
)


def prepare_window(config: Config, operations: Operations, review: ReviewCheckpoint) -> str:
    phase, gate_attempted = "PREFLIGHT", False
    receipt: Receipt | None = None
    try:
        validate_target(config)
        review.begin()
        receipt = Receipt(config, None)
        phase = cast(str, receipt.value["phase"])
        if receipt.value["status"] != "RUNNING" or phase not in {
            "PREFLIGHT",
            "GATED",
            "STOPPED",
            "SNAPSHOTTED",
        }:
            raise Unknown("review host attempt is not resumable")
        if receipt.existing:
            phase, proof = operations.reconcile_phase(phase, receipt.value, offline=False)
            if phase != receipt.value["phase"] or proof:
                receipt.value.update(proof)
                receipt.write(phase)
        else:
            receipt.write(phase)
            operations.preflight()
        if phase == "PREFLIGHT":
            gate_attempted = True
            operations.gate()
            phase = "GATED"
            receipt.write(phase)
        else:
            gate_attempted = True
        if phase == "GATED":
            if not operations.public_gated():
                raise Unknown("public maintenance gate is not exact")
            operations.stop()
            phase = "STOPPED"
            receipt.write(phase)
        if phase == "STOPPED":
            operations.snapshot()
            receipt.value["fastmcp_snapshot"] = operations.snapshot_digest()
            phase = "SNAPSHOTTED"
            receipt.write(phase)
        review.prepare()
        return "REVIEW_PENDING"
    except (Failed, Unknown, OSError, subprocess.SubprocessError) as error:
        if gate_attempted:
            try:
                retain_gate(operations)
            except Unknown:
                pass
        if receipt:
            try:
                receipt.write(phase, "UNKNOWN", type(error).__name__)
            except Unknown:
                pass
        return "UNKNOWN"


def status_window(
    config: Config, review: ReviewCheckpoint, offline: Stage12Cutover | None = None,
) -> str:
    try:
        state = review.status()
        try:
            receipt = Receipt(config, None).value
        except Unknown:
            if offline is None:
                raise
            receipt = Receipt(config, offline).value
    except (Failed, Unknown, OSError):
        return "UNKNOWN"
    if receipt["status"] in {"PASS", "FAIL"}:
        return cast(str, receipt["status"])
    if receipt["authority_crossed"]:
        return "POSTGRES_AUTHORITY_UNKNOWN" if receipt["status"] == "UNKNOWN" else "POSTGRES_AUTHORITY"
    if receipt["status"] == "UNKNOWN":
        return "UNKNOWN"
    if receipt["offline_receipt"] is not None:
        return cast(str, receipt["phase"])
    expected = {"REVIEW_PENDING": ("RUNNING", "SNAPSHOTTED"), "ABORTED": ("FAIL", "ROLLED_BACK")}
    return state if expected.get(state) == (receipt["status"], receipt["phase"]) else "UNKNOWN"


def resume_window(
    config: Config,
    operations: Operations,
    review: ReviewCheckpoint,
    evidence: Evidence,
    approved_worksheet_digest: str,
) -> str:
    gate_bound = False
    try:
        if review.status() != "REVIEW_PENDING":
            raise Unknown("Human Review checkpoint is not pending")
        commands = ConcreteCommands(config, evidence)
        offline = Stage12Cutover(config.attempt_dir, evidence, commands)
        try:
            receipt = Receipt(config, offline)
            if not receipt.existing:
                raise Unknown("review host receipt is absent")
            if receipt.value["status"] in {"PASS", "FAIL"}:
                return deploy(config, operations, offline)
            gate_bound = receipt.value["phase"] != "PREFLIGHT"
        except Unknown:
            receipt = Receipt(config, None)
            if (
                receipt.value["status"] != "RUNNING"
                or receipt.value["phase"] != "SNAPSHOTTED"
                or not operations.gate_exact()
                or not operations.public_gated()
            ):
                raise Unknown("review host receipt cannot adopt approved evidence")
            gate_bound = True
        observed = review.prepare()
        if observed != approved_worksheet_digest or evidence.worksheet_digest != observed:
            raise Failed("worksheet does not match the explicit Human Review approval")
        if receipt.value["offline_receipt"] is None:
            receipt.value.update(
                offline_receipt=str(offline.receipt_path),
                offline_candidate=offline.candidate_sha,
                offline_corpus=list(offline.corpus_manifests),
                offline_worksheet=offline.worksheet,
            )
            receipt.write("SNAPSHOTTED")
        return deploy(config, operations, offline)
    except (Failed, Unknown, OSError, subprocess.SubprocessError):
        if gate_bound:
            try:
                retain_gate(operations)
            except Unknown:
                pass
        return "UNKNOWN"


def abort_window(config: Config, operations: Operations, review: ReviewCheckpoint) -> str:
    phase = "PREFLIGHT"
    try:
        receipt = Receipt(config, None)
        phase = cast(str, receipt.value["phase"])
        if receipt.value["status"] == "FAIL":
            return "FAIL"
        if phase not in {"PREFLIGHT", "GATED", "STOPPED", "SNAPSHOTTED"}:
            raise Unknown("review host attempt crossed its abort boundary")
        review.abort()
        if phase in {"STOPPED", "SNAPSHOTTED"}:
            operations.start()
            if not operations.rollback_ready():
                raise Unknown("old runtime restart is not exact")
        if phase != "PREFLIGHT":
            operations.ungate()
            if not operations.public_ready():
                raise Unknown("old runtime public recovery is not exact")
        receipt.write("ROLLED_BACK", "FAIL")
        return "FAIL"
    except Failed, Unknown, OSError, subprocess.SubprocessError:
        if phase != "PREFLIGHT":
            try:
                retain_gate(operations)
            except Unknown:
                pass
        return "UNKNOWN"
