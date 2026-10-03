"""Offline failure queue, known-failure gate, and legacy-cutoff prerequisites."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from .failure_journal import FailureJournal, FailureRecord


class RegistrationState(StrEnum):
    UNRECORDED = "UNRECORDED"
    STORED = "STORED"
    PENDING_SYNC = "PENDING_SYNC"


class PendingFailureRegistry:
    def __init__(self) -> None:
        self._items: dict[UUID, RegistrationState] = {}

    def register(self, attempt_id: UUID) -> None:
        self._items.setdefault(attempt_id, RegistrationState.UNRECORDED)

    def mark(self, attempt_id: UUID, state: RegistrationState) -> None:
        if attempt_id not in self._items:
            raise KeyError("failure must be registered before recording its disposition")
        self._items[attempt_id] = state

    def closure_gate(self) -> tuple[bool, tuple[UUID, ...]]:
        blocked = tuple(
            key for key, state in self._items.items() if state == RegistrationState.UNRECORDED
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
                self._fsync_directory()
            finally:
                temporary.unlink(missing_ok=True)
        return "PENDING_SYNC"

    def _fsync_directory(self) -> None:
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def pending(self) -> tuple[FailureRecord, ...]:
        return tuple(
            FailureRecord.model_validate_json(path.read_bytes())
            for path in sorted(self.directory.glob("*.json"))
        )

    async def sync(self, journal: FailureJournal) -> tuple[UUID, ...]:
        synchronized: list[UUID] = []
        for value in self.pending():
            result = await journal.record(value)
            readback = await journal.get(value.attempt_id)
            if result.status not in {"APPLIED", "REPLAYED"} or readback != value:
                continue
            self._path(value.attempt_id).unlink(missing_ok=True)
            synchronized.append(value.attempt_id)
        if synchronized:
            self._fsync_directory()
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
