"""Canonical append-only failure evidence and resolution records."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self, cast
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Table, Text, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncEngine

from .contracts import ClosedModel
from .state import metadata

failure_records = Table(
    "failure_records",
    metadata,
    Column("attempt_id", PGUUID(as_uuid=True), primary_key=True),
    Column("operation_id", PGUUID(as_uuid=True), nullable=False, unique=True),
    Column("schema_version", BigInteger, nullable=False),
    Column("owner", Text, nullable=False),
    Column("attempted_claim", Text, nullable=False),
    Column("observed_result", Text, nullable=False),
    Column("clearing_action", Text, nullable=False),
    Column("effect_state", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("evidence", JSONB, nullable=False),
    Column("content_digest", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)
failure_resolutions = Table(
    "failure_resolutions",
    metadata,
    Column("resolution_id", PGUUID(as_uuid=True), primary_key=True),
    Column("operation_id", PGUUID(as_uuid=True), nullable=False, unique=True),
    Column(
        "attempt_id",
        PGUUID(as_uuid=True),
        ForeignKey("failure_records.attempt_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    Column("resolved_at", DateTime(timezone=True), nullable=False),
    Column("summary", Text, nullable=False),
    Column("evidence", JSONB, nullable=False),
    Column("content_digest", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

_SECRET = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+|((?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+|(?<=://)[^/@\s:]+:[^/@\s]+@"
)


def redact(value: str) -> str:
    """Remove common credentials before either durable path sees the value."""

    def replace(match: re.Match[str]) -> str:
        if match.group(1):
            return f"{match.group(1)}[redacted]"
        if match.group(2):
            return f"{match.group(2)}[redacted]"
        return "[redacted]@"

    return _SECRET.sub(replace, value)


def redact_environment(value: str, environment: dict[str, str]) -> str:
    """Redact syntax-based credentials plus exact secret values known at this boundary."""
    result = redact(value)
    for name, secret in environment.items():
        if secret and any(
            word in name.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "KEY")
        ):
            result = result.replace(secret, "[redacted]")
    return result


class EffectState(StrEnum):
    NOT_SENT = "NOT_SENT"
    APPLIED = "APPLIED"
    UNKNOWN = "UNKNOWN"


class FailureRecord(ClosedModel):
    schema_version: Literal[1] = 1
    attempt_id: UUID
    operation_id: UUID
    attempted_claim: str = Field(min_length=1, max_length=2000)
    observed_result: str = Field(min_length=1, max_length=4000)
    clearing_action: str = Field(min_length=1, max_length=2000)
    effect_state: EffectState
    owner: str = Field(min_length=1, max_length=36)
    occurred_at: datetime
    evidence: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("owner")
    @classmethod
    def owner_is_exact(cls, value: str) -> str:
        if value == "COORDINATOR":
            return value
        if str(UUID(value)) != value.lower():
            raise ValueError("owner must be COORDINATOR or a canonical WorkId")
        return value.lower()

    @field_validator("occurred_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bounded_evidence(self) -> Self:
        if any(not item or len(item) > 512 for item in self.evidence):
            raise ValueError("evidence pointers must contain 1..512 characters")
        return self

    def sanitized(self) -> Self:
        return self.model_copy(
            update={
                "attempted_claim": redact(self.attempted_claim),
                "observed_result": redact(self.observed_result),
                "clearing_action": redact(self.clearing_action),
                "evidence": tuple(redact(item) for item in self.evidence),
            }
        )

    def canonical(self) -> dict[str, object]:
        return self.sanitized().model_dump(mode="json")


class FailureResolution(ClosedModel):
    resolution_id: UUID
    operation_id: UUID
    attempt_id: UUID
    resolved_at: datetime
    summary: str = Field(min_length=1, max_length=2000)
    evidence: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("resolved_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("resolved_at must include a timezone")
        return value.astimezone(UTC)

    def canonical(self) -> dict[str, object]:
        if any(not item or len(item) > 512 for item in self.evidence):
            raise ValueError("evidence pointers must contain 1..512 characters")
        return self.model_copy(
            update={
                "summary": redact(self.summary),
                "evidence": tuple(map(redact, self.evidence)),
            }
        ).model_dump(mode="json")


@dataclass(frozen=True)
class JournalWrite:
    status: Literal["APPLIED", "REPLAYED", "CONFLICT", "UNKNOWN"]


def _digest(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class FailureJournal:
    """Canonical immutable records and separate append-only resolutions."""

    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def get(self, attempt_id: UUID) -> FailureRecord | Literal["UNKNOWN"] | None:
        async with self.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        select(failure_records).where(failure_records.c.attempt_id == attempt_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        try:
            record = FailureRecord.model_validate(
                {key: row[key] for key in FailureRecord.model_fields}
            )
            return record if _digest(record.canonical()) == row["content_digest"] else "UNKNOWN"
        except ValueError:
            return "UNKNOWN"

    async def attempt_ids(self) -> tuple[UUID, ...]:
        """Return immutable identities for import/read-parity comparisons."""
        async with self.engine.connect() as connection:
            values = (
                await connection.execute(
                    select(failure_records.c.attempt_id).order_by(failure_records.c.attempt_id)
                )
            ).scalars()
            return tuple(values)

    async def record(self, value: FailureRecord) -> JournalWrite:
        value = value.sanitized()
        digest = _digest(value.canonical())
        stored = {
            **value.model_dump(),
            "effect_state": value.effect_state.value,
            "evidence": list(value.evidence),
            "content_digest": digest,
        }
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    insert(failure_records)
                    .values(stored)
                    .on_conflict_do_nothing()
                    .returning(failure_records.c.content_digest)
                )
            ).scalar_one_or_none()
            if row is not None:
                return JournalWrite("APPLIED")
            existing = (
                (
                    await connection.execute(
                        select(failure_records.c.content_digest).where(
                            or_(
                                failure_records.c.attempt_id == value.attempt_id,
                                failure_records.c.operation_id == value.operation_id,
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
        if len(existing) != 1:
            return JournalWrite("UNKNOWN")
        return JournalWrite("REPLAYED" if existing[0] == digest else "CONFLICT")

    async def resolve(self, value: FailureResolution) -> JournalWrite:
        payload, digest = value.canonical(), _digest(value.canonical())
        stored = {
            **value.model_dump(),
            "summary": cast(str, payload["summary"]),
            "evidence": cast(list[str], payload["evidence"]),
            "content_digest": digest,
        }
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    insert(failure_resolutions)
                    .values(stored)
                    .on_conflict_do_nothing()
                    .returning(failure_resolutions.c.content_digest)
                )
            ).scalar_one_or_none()
            if row is not None:
                return JournalWrite("APPLIED")
            existing = (
                (
                    await connection.execute(
                        select(failure_resolutions.c.content_digest).where(
                            or_(
                                failure_resolutions.c.resolution_id == value.resolution_id,
                                failure_resolutions.c.operation_id == value.operation_id,
                                failure_resolutions.c.attempt_id == value.attempt_id,
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
        if len(existing) != 1:
            return JournalWrite("UNKNOWN")
        return JournalWrite("REPLAYED" if existing[0] == digest else "CONFLICT")

    async def open(
        self, *, owner: str | None = None
    ) -> tuple[FailureRecord, ...] | Literal["UNKNOWN"]:
        statement = (
            select(failure_records.c.attempt_id)
            .outerjoin(failure_resolutions)
            .where(failure_resolutions.c.attempt_id.is_(None))
            .order_by(failure_records.c.occurred_at, failure_records.c.attempt_id)
        )
        if owner is not None:
            statement = statement.where(failure_records.c.owner == owner)
        async with self.engine.connect() as connection:
            ids = (await connection.execute(statement)).scalars().all()
        values = [await self.get(item) for item in ids]
        if any(value in (None, "UNKNOWN") for value in values):
            return "UNKNOWN"
        return tuple(cast(FailureRecord, value) for value in values)
