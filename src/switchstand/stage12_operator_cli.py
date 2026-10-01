"""Explicit target-bound CLI for the inert Stage 1+2 operator core."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import argparse
import signal
from pathlib import Path
from typing import Any, cast

from .edge_maintenance import (
    FASTMCP_STATE,
    LOCK,
    Config,
    HostOperations,
    Interrupted,
    Unknown,
    _exclusive_lock,
    _validate_target,
)
from .rehearsal_target import load_ready, operator_lock
from .stage12_cutover import (
    ConcreteCommands,
    Evidence,
    ReviewCheckpoint,
    ReviewEvidence,
    Stage12Cutover,
)
from .stage12_operator import abort_window, prepare_window, resume_window, status_window


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "status", "resume", "abort"))
    parser.add_argument("--target", required=True, help="production or disposable:NAME")
    for name in (
        "attempt-dir", "current-runtime", "candidate-runtime", "candidate-launcher",
        "launcher", "env-file",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--fastmcp-state", type=Path)
    for name in (
        "current-sha", "candidate-sha", "candidate-launcher-sha", "current-launcher-sha",
        "expected-corpus-digest", "exception-digest",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--manifest", action="append", type=Path, required=True)
    parser.add_argument("--worksheet", type=Path, required=True)
    parser.add_argument("--approved-worksheet-digest")
    parser.add_argument("--minimum-free-bytes", type=int, default=1)
    parser.add_argument("--existing-attempt", action="store_true")
    return parser


def _config(args: argparse.Namespace) -> Config:
    common: dict[str, Any] = {}
    if args.target == "production":
        if args.fastmcp_state != FASTMCP_STATE:
            raise SystemExit(f"production requires --fastmcp-state {FASTMCP_STATE}")
        target, state, lock = "production", FASTMCP_STATE, LOCK
    elif args.target.startswith("disposable:") and args.target.count(":") == 1:
        name = args.target.split(":", 1)[1]
        root, descriptor = load_ready(name, args.candidate_sha, args.candidate_runtime)
        endpoints = cast(dict[str, str], descriptor["endpoints"])
        target, state, lock = "disposable", Path(descriptor["fastmcp_state"]), root / "operator.lock"
        if args.fastmcp_state is not None and args.fastmcp_state != state:
            raise SystemExit("disposable FastMCP state must match its READY descriptor")
        common = {
            "target_root": root,
            "service": descriptor["edge_service"],
            "caddy": endpoints["caddy"],
            "local_url": endpoints["local"],
            "public_origin": endpoints["public"],
        }
    else:
        raise SystemExit("--target must be exactly production or disposable:NAME")
    config = Config(
        args.attempt_dir, args.current_runtime, args.current_sha, args.candidate_runtime,
        args.candidate_sha, args.candidate_launcher, args.candidate_launcher_sha,
        args.launcher, args.current_launcher_sha, state, args.env_file, target,
        lock_path=lock, **common,
    )
    _validate_target(config)
    return config


def _target_lock(target: str) -> Path:
    if target == "production":
        return LOCK
    if target.startswith("disposable:") and target.count(":") == 1:
        return operator_lock(target.split(":", 1)[1])
    raise SystemExit("--target must be exactly production or disposable:NAME")


def _evidence(args: argparse.Namespace) -> tuple[ReviewEvidence, Evidence | None]:
    if len(args.manifest) != 2:
        raise SystemExit("exactly two --manifest values are required")
    manifests = cast(tuple[Path, Path], tuple(args.manifest))
    review = ReviewEvidence(
        args.candidate_sha, manifests, args.expected_corpus_digest,
        args.exception_digest, args.worksheet,
    )
    approved = Evidence(
        args.candidate_sha, manifests, args.expected_corpus_digest, args.exception_digest,
        args.worksheet, args.approved_worksheet_digest, args.minimum_free_bytes,
    ) if args.approved_worksheet_digest else None
    return review, approved


def _attempt(args: argparse.Namespace, config: Config) -> None:
    if args.action == "prepare" and args.existing_attempt:
        if not config.attempt_dir.is_dir():
            raise SystemExit("--existing-attempt requires the exact existing attempt")
    elif args.action == "prepare":
        config.attempt_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    elif args.existing_attempt:
        raise SystemExit("--existing-attempt is valid only with prepare")


def _exit(result: str) -> int:
    return 2 if result == "UNKNOWN" or result.endswith("_UNKNOWN") else 1 if result == "FAIL" else 0


def _run(args: argparse.Namespace) -> str:
    config = _config(args)
    review_evidence, evidence = _evidence(args)
    commands = ConcreteCommands(config, review_evidence)
    review = ReviewCheckpoint(config.attempt_dir, review_evidence, commands)
    if args.action == "status":
        offline = Stage12Cutover(config.attempt_dir, evidence, ConcreteCommands(config, evidence)) if evidence else None
        return status_window(config, review, offline)
    _attempt(args, config)

    def interrupted(number: int, _frame: object) -> None:
        raise Interrupted(f"operator interrupted by signal {number}")

    previous = {item: signal.signal(item, interrupted) for item in (signal.SIGINT, signal.SIGTERM)}
    try:
        operations = HostOperations(config)
        if args.action == "prepare":
            return prepare_window(config, operations, review)
        if args.action == "abort":
            return abort_window(config, operations, review)
        if evidence is None:
            raise SystemExit("resume requires --approved-worksheet-digest")
        return resume_window(config, operations, review, evidence, args.approved_worksheet_digest)
    finally:
        for item, handler in previous.items():
            signal.signal(item, handler)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.action in {"prepare", "abort"} and args.approved_worksheet_digest:
        raise SystemExit("approved worksheet evidence is valid only for status or resume")
    if args.action == "status" and args.existing_attempt:
        raise SystemExit("--existing-attempt is valid only with prepare")
    try:
        with _exclusive_lock(_target_lock(args.target)):
            result = _run(args)
    except Unknown:
        result = "UNKNOWN"
    print(result)
    return _exit(result)


if __name__ == "__main__":
    raise SystemExit(main())
