"""Exact evidence and durable boundaries for the offline Stage 1+2 cutover."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from .edge_maintenance import Failed, Unknown
from .work_corpus import load_manifest, manifest_exception_digest


class CutoverCommands(Protocol):
    """Effects supplied by the concrete command adapter after A2 is fixed."""

    def upgrade_schema(self, receipt: Path) -> Path: ...
    def prepare_stage1(self) -> str: ...
    def capture_final_corpus(self, destination: Path) -> None: ...
    def activate_stage1(self, prepared_digest: str, receipt: Path) -> None: ...
    def validate_stage2(self) -> None: ...
    def activate_stage2(self, receipt: Path) -> None: ...
    def abort_schema(self, receipt: Path) -> None: ...


@dataclass(frozen=True)
class Evidence:
    candidate_sha: str
    manifests: tuple[Path, Path]
    exception_digest: str
    worksheet: Path
    worksheet_digest: str
    minimum_free_bytes: int


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _private_file(path: Path, label: str) -> None:
    try:
        stat = path.lstat()
    except OSError as error:
        raise Failed(f"{label} is unavailable") from error
    if path.is_symlink() or not path.is_file() or stat.st_mode & 0o777 != 0o600:
        raise Failed(f"{label} is not an exact mode-0600 regular file")


def validate_evidence(evidence: Evidence, attempt_dir: Path) -> tuple[str, str]:
    """Validate immutable operator inputs before any database effect."""
    if len(evidence.candidate_sha) != 40 or any(
        character not in "0123456789abcdef" for character in evidence.candidate_sha
    ):
        raise Failed("candidate SHA is invalid")
    if evidence.minimum_free_bytes < 1:
        raise Failed("minimum free-space requirement is invalid")
    for path, label in (
        (evidence.manifests[0], "first corpus manifest"),
        (evidence.manifests[1], "second corpus manifest"),
        (evidence.worksheet, "Stage 2 worksheet"),
    ):
        _private_file(path, label)
    try:
        first, second = (load_manifest(path) for path in evidence.manifests)
        if first != second:
            raise Failed("the two reviewed corpus manifests do not match")
        if first.get("source_candidate") != evidence.candidate_sha:
            raise Failed("corpus manifests do not bind the exact candidate")
        if manifest_exception_digest(first) != evidence.exception_digest:
            raise Failed("corpus exceptions do not match the reviewed digest")
    except (OSError, TypeError, ValueError) as error:
        raise Failed("corpus evidence is invalid") from error
    worksheet_digest = _digest(evidence.worksheet)
    if worksheet_digest != evidence.worksheet_digest:
        raise Failed("Stage 2 worksheet digest does not match")
    if shutil.disk_usage(attempt_dir).free < evidence.minimum_free_bytes:
        raise Failed("insufficient free space for the offline cutover")
    return cast(str, first["sha256"]), worksheet_digest


class Stage12Cutover:
    """C1 OfflineStep which composes exact Stage 1+2 migration primitives."""

    def __init__(
        self,
        attempt_dir: Path,
        evidence: Evidence,
        commands: CutoverCommands,
    ):
        self.receipt_path = attempt_dir / "stage12-cutover.json"
        self._attempt_dir = attempt_dir
        self._evidence = evidence
        self._commands = commands
        self._schema_receipt = attempt_dir / "schema-upgrade.json"
        self._stage1_receipt = attempt_dir / "stage1-activation.json"
        self._stage2_receipt = attempt_dir / "stage2-activation.json"
        self._final_manifest = attempt_dir / "corpus-final.json"
        self.database_backup = "PENDING"
        self.corpus_manifests = ("PENDING", "PENDING")
        self.worksheet = "PENDING"

    def _write(self, boundary: str, **subordinate: str) -> None:
        payload: dict[str, object] = {
            "schema_version": 1,
            "candidate_sha": self._evidence.candidate_sha,
            "database_backup": self.database_backup,
            "corpus_manifests": list(self.corpus_manifests),
            "worksheet": self.worksheet,
            "exception_digest": self._evidence.exception_digest,
            "terminal_boundary": boundary,
            **subordinate,
        }
        temporary = self.receipt_path.with_suffix(".tmp")
        data = (json.dumps(payload, sort_keys=True) + "\n").encode()
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.replace(temporary, self.receipt_path)
        directory = os.open(self.receipt_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def run(self, advance: Callable[[str], None]) -> None:
        manifest_digest, self.worksheet = validate_evidence(self._evidence, self._attempt_dir)
        self.corpus_manifests = (manifest_digest, manifest_digest)
        for path in (
            self.receipt_path,
            self._schema_receipt,
            self._stage1_receipt,
            self._stage2_receipt,
            self._final_manifest,
        ):
            if path.exists():
                raise Failed("an offline cutover attempt artifact already exists")

        backup = self._commands.upgrade_schema(self._schema_receipt)
        _private_file(self._schema_receipt, "schema upgrade receipt")
        _private_file(backup, "database backup")
        self.database_backup = _digest(backup)
        self._write("PRE_MARKER", schema_receipt=_digest(self._schema_receipt))
        advance("PRE_MARKER")

        prepared = self._commands.prepare_stage1()
        self._commands.capture_final_corpus(self._final_manifest)
        _private_file(self._final_manifest, "final corpus manifest")
        try:
            final = load_manifest(self._final_manifest)
        except (OSError, TypeError, ValueError) as error:
            raise Failed("final corpus manifest is invalid") from error
        if final.get("sha256") != manifest_digest:
            raise Failed("final corpus no longer matches the reviewed manifests")
        self._commands.activate_stage1(prepared, self._stage1_receipt)
        _private_file(self._stage1_receipt, "Stage 1 activation receipt")
        self._write(
            "POSTGRES_AUTHORITY",
            schema_receipt=_digest(self._schema_receipt),
            final_manifest=_digest(self._final_manifest),
            stage1_receipt=_digest(self._stage1_receipt),
        )
        advance("POSTGRES_AUTHORITY")

        self._commands.validate_stage2()
        self._commands.activate_stage2(self._stage2_receipt)
        _private_file(self._stage2_receipt, "Stage 2 activation receipt")
        self._write(
            "COMPLETE",
            schema_receipt=_digest(self._schema_receipt),
            final_manifest=_digest(self._final_manifest),
            stage1_receipt=_digest(self._stage1_receipt),
            stage2_receipt=_digest(self._stage2_receipt),
        )
        advance("COMPLETE")

    def abort_pre_authority(self) -> None:
        try:
            self._commands.abort_schema(self._schema_receipt)
        except Failed as error:
            raise Unknown("pre-authority schema abort was not proven") from error
