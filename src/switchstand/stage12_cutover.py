"""Immutable operator evidence for the offline Stage 1+2 cutover."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .edge_maintenance import Failed
from .work_corpus import load_manifest, manifest_exception_digest


@dataclass(frozen=True)
class Evidence:
    candidate_sha: str
    manifests: tuple[Path, Path]
    expected_corpus_digest: str
    exception_digest: str
    worksheet: Path
    worksheet_digest: str
    minimum_free_bytes: int


@dataclass(frozen=True)
class FrozenEvidence:
    candidate_sha: str
    manifests: tuple[Path, Path]
    corpus_digest: str
    exception_digest: str
    worksheet: Path
    worksheet_digest: str


def _read_private(path: Path, label: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise Failed(f"{label} is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o777 != 0o600:
            raise Failed(f"{label} is not an exact mode-0600 regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _publish_or_match(path: Path, data: bytes, label: str) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if _read_private(path, label) != data:
            raise Failed(f"existing {label} does not match this attempt") from None
        return
    except OSError as error:
        raise Failed(f"cannot freeze {label}") from error
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def freeze_evidence(evidence: Evidence, attempt_dir: Path) -> FrozenEvidence:
    """Validate and immutably copy exact pre-effect evidence into an attempt."""
    if len(evidence.candidate_sha) != 40 or any(
        character not in "0123456789abcdef" for character in evidence.candidate_sha
    ):
        raise Failed("candidate SHA is invalid")
    if evidence.minimum_free_bytes < 1:
        raise Failed("minimum free-space requirement is invalid")
    try:
        attempt = attempt_dir.lstat()
    except OSError as error:
        raise Failed("attempt directory is unavailable") from error
    if (
        attempt_dir.is_symlink()
        or not attempt_dir.is_dir()
        or attempt.st_uid != os.getuid()
        or attempt.st_mode & 0o777 != 0o700
    ):
        raise Failed("attempt directory identity or mode is invalid")
    if shutil.disk_usage(attempt_dir).free < evidence.minimum_free_bytes:
        raise Failed("insufficient free space for the offline cutover")

    sources = (
        (evidence.manifests[0], attempt_dir / "corpus-a.json", "first corpus manifest"),
        (evidence.manifests[1], attempt_dir / "corpus-b.json", "second corpus manifest"),
        (evidence.worksheet, attempt_dir / "stage2-worksheet.json", "Stage 2 worksheet"),
    )
    for source, destination, label in sources:
        _publish_or_match(destination, _read_private(source, label), label)
    directory = os.open(attempt_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)

    frozen_manifests = cast(tuple[Path, Path], tuple(item[1] for item in sources[:2]))
    try:
        first, second = (load_manifest(path) for path in frozen_manifests)
        if first != second:
            raise Failed("the two reviewed corpus manifests do not match")
        if first.get("source_candidate") != evidence.candidate_sha:
            raise Failed("corpus manifests do not bind the exact candidate")
        if first.get("sha256") != evidence.expected_corpus_digest:
            raise Failed("corpus manifest does not match the expected full digest")
        if manifest_exception_digest(first) != evidence.exception_digest:
            raise Failed("corpus exceptions do not match the reviewed digest")
    except (OSError, TypeError, ValueError) as error:
        raise Failed("corpus evidence is invalid") from error
    frozen_worksheet = sources[2][1]
    worksheet_digest = hashlib.sha256(
        _read_private(frozen_worksheet, "frozen Stage 2 worksheet")
    ).hexdigest()
    if worksheet_digest != evidence.worksheet_digest:
        raise Failed("Stage 2 worksheet digest does not match")
    return FrozenEvidence(
        evidence.candidate_sha,
        frozen_manifests,
        evidence.expected_corpus_digest,
        evidence.exception_digest,
        frozen_worksheet,
        evidence.worksheet_digest,
    )
