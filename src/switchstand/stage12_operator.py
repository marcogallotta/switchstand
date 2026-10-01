"""Explicit two-step host operator for the Stage 1+2 review checkpoint."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import argparse
import signal
import subprocess
from pathlib import Path
from typing import cast

from .edge_maintenance import (
    CADDY,
    LOCAL_URL,
    LOCK,
    PUBLIC_ORIGIN,
    SERVICE,
    Config,
    Failed,
    HostOperations,
    Operations,
    Receipt,
    Unknown,
    deploy,
)
from .edge_maintenance import (
    _exclusive_lock as exclusive_lock,
)
from .edge_maintenance import (
    _retain_gate as retain_gate,
)
from .edge_maintenance import (
    _validate_target as validate_target,
)
from .stage12_cutover import (
    ConcreteCommands,
    Evidence,
    ReviewCheckpoint,
    ReviewEvidence,
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


def status_window(config: Config, review: ReviewCheckpoint) -> str:
    try:
        state = review.status()
        receipt = Receipt(config, None).value
    except (Failed, Unknown, OSError):
        return "UNKNOWN"
    expected = {"REVIEW_PENDING": ("RUNNING", "SNAPSHOTTED"), "ABORTED": ("FAIL", "ROLLED_BACK")}
    return state if expected.get(state) == (receipt["status"], receipt["phase"]) else "UNKNOWN"


def resume_window(
    config: Config,
    operations: Operations,
    review: ReviewCheckpoint,
    evidence: Evidence,
    approved_worksheet_digest: str,
) -> str:
    try:
        observed = review.prepare()
        if observed != approved_worksheet_digest or evidence.worksheet_digest != observed:
            raise Failed("worksheet does not match the explicit Human Review approval")
        commands = ConcreteCommands(config, evidence)
        offline = Stage12Cutover(config.attempt_dir, evidence, commands)
        try:
            Receipt(config, offline)
        except Unknown:
            receipt = Receipt(config, None)
            if receipt.value["status"] != "RUNNING" or receipt.value["phase"] != "SNAPSHOTTED":
                raise Unknown("review host receipt cannot adopt approved evidence")
            receipt.value.update(
                offline_receipt=str(offline.receipt_path),
                offline_candidate=offline.candidate_sha,
                offline_corpus=list(offline.corpus_manifests),
                offline_worksheet=offline.worksheet,
            )
            receipt.write("SNAPSHOTTED")
        return deploy(config, operations, offline)
    except (Failed, Unknown, OSError, subprocess.SubprocessError):
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "status", "resume", "abort"))
    parser.add_argument("--target", required=True)
    for name in (
        "attempt-dir",
        "current-runtime",
        "candidate-runtime",
        "candidate-launcher",
        "launcher",
        "fastmcp-state",
        "env-file",
        "lock-path",
    ):
        parser.add_argument("--" + name, type=Path, required=name != "lock-path")
    for name in (
        "current-sha",
        "candidate-sha",
        "candidate-launcher-sha",
        "current-launcher-sha",
        "expected-corpus-digest",
        "exception-digest",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--manifest", action="append", type=Path, required=True)
    parser.add_argument("--worksheet", type=Path, required=True)
    parser.add_argument("--approved-worksheet-digest")
    parser.add_argument("--minimum-free-bytes", type=int, default=1)
    parser.add_argument("--service", default=SERVICE)
    parser.add_argument("--caddy", default=CADDY)
    parser.add_argument("--local-url", default=LOCAL_URL)
    parser.add_argument("--public-origin", default=PUBLIC_ORIGIN)
    parser.add_argument("--target-root", type=Path)
    parser.add_argument("--existing-attempt", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if len(args.manifest) != 2 or not (
        args.target == "production" or args.target.startswith("disposable:")
    ):
        raise SystemExit(
            "exactly two manifests and an explicit production|disposable:NAME target are required"
        )
    target = "production" if args.target == "production" else "disposable"
    if target == "disposable" and (
        args.target_root is None or args.target != f"disposable:{args.target_root.name}"
    ):
        raise SystemExit("disposable target name must match --target-root")
    config = Config(
        args.attempt_dir,
        args.current_runtime,
        args.current_sha,
        args.candidate_runtime,
        args.candidate_sha,
        args.candidate_launcher,
        args.candidate_launcher_sha,
        args.launcher,
        args.current_launcher_sha,
        args.fastmcp_state,
        args.env_file,
        target,
        caddy=args.caddy,
        public_origin=args.public_origin,
        target_root=args.target_root,
        service=args.service,
        local_url=args.local_url,
        lock_path=args.lock_path or LOCK,
    )
    validate_target(config)
    if args.action == "prepare":
        if args.existing_attempt:
            if not config.attempt_dir.is_dir():
                raise SystemExit("--existing-attempt requires the exact existing attempt directory")
        else:
            config.attempt_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    elif args.existing_attempt:
        raise SystemExit("--existing-attempt is valid only with prepare")
    review_evidence = ReviewEvidence(
        args.candidate_sha,
        cast(tuple[Path, Path], tuple(args.manifest)),
        args.expected_corpus_digest,
        args.exception_digest,
        args.worksheet,
    )
    commands = ConcreteCommands(config, review_evidence)
    review = ReviewCheckpoint(config.attempt_dir, review_evidence, commands)
    if args.action == "status":
        result = status_window(config, review)
        print(result)
        return 0 if result != "UNKNOWN" else 2
    with exclusive_lock(config.lock_path):
        previous = signal.signal(
            signal.SIGTERM,
            lambda number, _frame: (_ for _ in ()).throw(
                Unknown(f"maintenance interrupted by signal {number}")
            ),
        )
        try:
            if args.action == "prepare":
                result = prepare_window(config, HostOperations(config), review)
            elif args.action == "abort":
                result = abort_window(config, HostOperations(config), review)
            else:
                if not args.approved_worksheet_digest:
                    raise SystemExit("resume requires --approved-worksheet-digest")
                evidence = Evidence(
                    args.candidate_sha,
                    review_evidence.manifests,
                    args.expected_corpus_digest,
                    args.exception_digest,
                    args.worksheet,
                    args.approved_worksheet_digest,
                    args.minimum_free_bytes,
                )
                result = resume_window(
                    config,
                    HostOperations(config),
                    review,
                    evidence,
                    args.approved_worksheet_digest,
                )
        finally:
            signal.signal(signal.SIGTERM, previous)
    print(result)
    return {"REVIEW_PENDING": 0, "PASS": 0, "FAIL": 1, "UNKNOWN": 2}[result]


if __name__ == "__main__":
    raise SystemExit(main())
