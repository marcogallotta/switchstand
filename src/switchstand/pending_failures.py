"""Offline failure queue, known-failure gate, and legacy-cutoff prerequisites."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

from .failure_journal import FailureJournal, FailureRecord


class RegistrationState(StrEnum):
    UNRECORDED = "UNRECORDED"
    STORED = "STORED"
    PENDING_SYNC = "PENDING_SYNC"


class PendingFailureRegistry:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory
        self._items: dict[UUID, RegistrationState] = {}
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def _locked_items(self) -> Generator[dict[UUID, RegistrationState]]:
        if self.directory is None:
            yield self._items
            return
        descriptor = os.open(self.directory / ".registry.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            path = self.directory / "registry.json"
            raw = {} if not path.exists() else cast(dict[str, str], json.loads(path.read_text()))
            items = {UUID(key): RegistrationState(value) for key, value in raw.items()}
            yield items
            temporary = self.directory / f".registry.{uuid4()}.tmp"
            output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(output, "w") as stream:
                json.dump({str(key): value.value for key, value in items.items()}, stream,
                          sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            PendingFailureQueue(self.directory).fsync_directory()
        finally:
            os.close(descriptor)

    def register(self, attempt_id: UUID) -> None:
        with self._locked_items() as items:
            items.setdefault(attempt_id, RegistrationState.UNRECORDED)

    def mark(self, attempt_id: UUID, state: RegistrationState) -> None:
        with self._locked_items() as items:
            if attempt_id not in items:
                raise KeyError("failure must be registered before recording its disposition")
            items[attempt_id] = state

    def closure_gate(self) -> tuple[bool, tuple[UUID, ...]]:
        with self._locked_items() as items:
            if self.directory is not None:
                for value in PendingFailureQueue(self.directory).pending():
                    items.setdefault(value.attempt_id, RegistrationState.PENDING_SYNC)
            blocked = tuple(
                key for key, state in items.items() if state == RegistrationState.UNRECORDED
            )
        return not blocked, blocked


class PendingFailureQueue:
    """Atomic mode-0600 queue; successful replay is removed only after exact readback."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, attempt_id: UUID) -> Path:
        return self.directory / f"{attempt_id}.json"

    @contextmanager
    def _lock(self) -> Generator[None]:
        descriptor = os.open(self.directory / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def enqueue(self, value: FailureRecord) -> Literal["PENDING_SYNC", "CONFLICT"]:
        value = value.sanitized()
        data = json.dumps(value.canonical(), sort_keys=True, separators=(",", ":")).encode()
        target, temporary = self._path(value.attempt_id), self.directory / f".{uuid4()}.tmp"
        with self._lock():
            if target.exists():
                return "PENDING_SYNC" if target.read_bytes() == data else "CONFLICT"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
                self.fsync_directory()
            finally:
                temporary.unlink(missing_ok=True)
        return "PENDING_SYNC"

    def fsync_directory(self) -> None:
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def pending(self) -> tuple[FailureRecord, ...]:
        values: list[FailureRecord] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                UUID(path.stem)
            except ValueError:
                continue
            values.append(FailureRecord.model_validate_json(path.read_bytes()))
        return tuple(values)

    def frames(self) -> tuple[str, ...]:
        """Snapshot sanitized immutable records for the controller stdin boundary."""
        frames: list[str] = []
        with self._lock():
            for value in self.pending():
                payload = value.canonical()
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                frames.append(json.dumps({
                    "record": payload, "digest": hashlib.sha256(encoded).hexdigest()
                }, sort_keys=True, separators=(",", ":")))
        return tuple(frames)

    def acknowledge(
        self, attempt_id: UUID, operation_id: UUID, digest: str,
        registry: PendingFailureRegistry | None = None,
    ) -> bool:
        """Delete only an exact host record acknowledged after canonical DB readback."""
        registry = registry or PendingFailureRegistry(self.directory)
        with self._lock():
            path = self._path(attempt_id)
            if not path.exists():
                return False
            value = FailureRecord.model_validate_json(path.read_bytes())
            encoded = json.dumps(
                value.canonical(), sort_keys=True, separators=(",", ":")
            ).encode()
            if value.operation_id != operation_id or hashlib.sha256(encoded).hexdigest() != digest:
                return False
            registry.register(attempt_id)
            registry.mark(attempt_id, RegistrationState.STORED)
            path.unlink()
            self.fsync_directory()
            return True

    async def sync(
        self, journal: FailureJournal, registry: PendingFailureRegistry | None = None
    ) -> tuple[UUID, ...]:
        registry = registry or PendingFailureRegistry(self.directory)
        synchronized: list[UUID] = []
        for value in self.pending():
            result = await journal.record(value)
            readback = await journal.get(value.attempt_id)
            if result.status not in {"APPLIED", "REPLAYED"} or readback != value:
                continue
            registry.register(value.attempt_id)
            registry.mark(value.attempt_id, RegistrationState.STORED)
            self._path(value.attempt_id).unlink(missing_ok=True)
            synchronized.append(value.attempt_id)
        if synchronized:
            self.fsync_directory()
        return tuple(synchronized)


def legacy_cutoff_readiness(
    *, source_ids: set[UUID], imported_ids: set[UUID], read_parity: bool, queue_empty: bool
) -> tuple[bool, tuple[str, ...]]:
    blockers: list[str] = []
    if source_ids != imported_ids:
        blockers.append("IMPORT_INCOMPLETE")
    if not read_parity:
        blockers.append("READ_PARITY_UNPROVED")
    if not queue_empty:
        blockers.append("PENDING_SYNC_REMAINS")
    return not blockers, tuple(blockers)


def failure_queue_root(home: Path) -> Path:
    return home / ".local/state/switchstand/failures/pending"
