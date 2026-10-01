"""Inert durable human trajectory storage; this module grants no effect authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from .state import human_trajectory_revisions, work_handles


class SourceKind(StrEnum):
    HUMAN_INPUT = "HUMAN_INPUT"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    HUMAN_STEERING = "HUMAN_STEERING"


class HumanTrajectoryData(BaseModel):
    """The closed V1 semantic-continuity payload, never an authorization record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    outcome: str = Field(min_length=1, max_length=2000)
    settled_decisions: tuple[str, ...] = Field(default=(), max_length=32)
    accepted_cuts_or_deferrals: tuple[str, ...] = Field(default=(), max_length=32)
    unresolved_human_questions: tuple[str, ...] = Field(default=(), max_length=32)
    current_slice: str = Field(min_length=1, max_length=2000)
    remaining_outcome: str = Field(max_length=2000)
    authority_effect_refs: tuple[str, ...] = Field(default=(), max_length=32)
    provenance: Literal["RECORDED_HUMAN_DIRECTION"] = "RECORDED_HUMAN_DIRECTION"

    @classmethod
    def _bounded_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or len(value) > 512 for value in values):
            raise ValueError("trajectory list items must contain 1..512 characters")
        return values

    def canonical(self) -> dict[str, object]:
        for value in (
            self.settled_decisions, self.accepted_cuts_or_deferrals,
            self.unresolved_human_questions, self.authority_effect_refs,
        ):
            self._bounded_items(value)
        return self.model_dump(mode="json")


@dataclass(frozen=True)
class HumanTrajectoryRevision:
    trajectory_id: UUID
    append_request_id: UUID
    work_id: UUID
    generation: int
    predecessor_id: UUID | None
    source_kind: SourceKind
    source_ref: str
    source_revision: str | None
    content_digest: str
    data: HumanTrajectoryData
    created_at: datetime


@dataclass(frozen=True)
class TrajectoryRead:
    status: Literal["CURRENT", "MISSING", "UNKNOWN"]
    head: HumanTrajectoryRevision | None = None


@dataclass(frozen=True)
class TrajectoryCheck:
    status: Literal["CURRENT", "STALE", "UNKNOWN"]
    current_id: UUID | None = None


@dataclass(frozen=True)
class TrajectoryRecord:
    status: Literal["APPLIED", "REPLAYED", "STALE", "CONFLICT", "UNKNOWN"]
    revision: HumanTrajectoryRevision | None = None
    current_id: UUID | None = None


_COLUMNS = tuple(human_trajectory_revisions.c)


def _row(value: tuple[object, ...]) -> HumanTrajectoryRevision:
    return HumanTrajectoryRevision(
        trajectory_id=cast(UUID, value[0]), append_request_id=cast(UUID, value[1]),
        work_id=cast(UUID, value[2]), generation=cast(int, value[3]),
        predecessor_id=cast(UUID | None, value[4]), source_kind=SourceKind(cast(str, value[5])),
        source_ref=cast(str, value[6]), source_revision=cast(str | None, value[7]),
        content_digest=cast(str, value[8]), data=HumanTrajectoryData.model_validate(value[9]),
        created_at=cast(datetime, value[10]),
    )


def _digest(
    data: HumanTrajectoryData, source_kind: SourceKind,
    source_ref: str, source_revision: str | None,
) -> str:
    canonical = {
        "data": data.canonical(), "source_kind": source_kind.value,
        "source_ref": source_ref, "source_revision": source_revision,
    }
    return hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


def _chain(values: list[tuple[object, ...]]) -> tuple[HumanTrajectoryRevision, ...] | None:
    try:
        rows = tuple(_row(value) for value in values)
        predecessor: UUID | None = None
        for generation, row in enumerate(rows, 1):
            if (
                row.generation != generation or row.predecessor_id != predecessor
                or row.content_digest != _digest(
                    row.data, row.source_kind, row.source_ref, row.source_revision
                )
            ):
                return None
            predecessor = row.trajectory_id
        return rows
    except (TypeError, ValueError, ValidationError):
        return None


class HumanTrajectoryStore:
    """Internal append/read/check owner; deliberately absent from public MCP wiring."""

    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def _load(self, work_id: UUID) -> tuple[HumanTrajectoryRevision, ...] | None:
        async with self.engine.connect() as connection:
            values = (await connection.execute(select(*_COLUMNS).where(
                human_trajectory_revisions.c.work_id_ref == work_id
            ).order_by(human_trajectory_revisions.c.generation))).all()
        return _chain([tuple(value) for value in values])

    async def get(self, work_id: UUID) -> TrajectoryRead:
        rows = await self._load(work_id)
        if rows == ():
            return TrajectoryRead("MISSING")
        if rows is None:
            return TrajectoryRead("UNKNOWN")
        return TrajectoryRead("CURRENT", rows[-1])

    async def check(self, work_id: UUID, expected_trajectory_id: UUID) -> TrajectoryCheck:
        rows = await self._load(work_id)
        if not rows:
            return TrajectoryCheck("UNKNOWN")
        if rows[-1].trajectory_id == expected_trajectory_id:
            return TrajectoryCheck("CURRENT", rows[-1].trajectory_id)
        if any(row.trajectory_id == expected_trajectory_id for row in rows[:-1]):
            return TrajectoryCheck("STALE", rows[-1].trajectory_id)
        return TrajectoryCheck("UNKNOWN", rows[-1].trajectory_id)

    async def record(
        self, *, work_id: UUID, append_request_id: UUID, expected_generation: int,
        expected_predecessor_id: UUID | None, source_kind: SourceKind, source_ref: str,
        source_revision: str | None, data: HumanTrajectoryData,
    ) -> TrajectoryRecord:
        if expected_generation < 1:
            raise ValueError("expected_generation must be positive")
        if not source_ref or len(source_ref) > 512:
            raise ValueError("source_ref must contain 1..512 characters")
        if source_revision is not None and (not source_revision or len(source_revision) > 512):
            raise ValueError("source_revision must contain 1..512 characters")
        digest = _digest(data, source_kind, source_ref, source_revision)
        async with self.engine.begin() as connection:
            handle = (await connection.execute(select(work_handles.c.id).where(
                work_handles.c.id == work_id
            ).with_for_update())).scalar_one_or_none()
            if handle is None:
                return TrajectoryRecord("UNKNOWN")
            values = (await connection.execute(select(*_COLUMNS).where(
                human_trajectory_revisions.c.work_id_ref == work_id
            ).order_by(human_trajectory_revisions.c.generation))).all()
            chain = _chain([tuple(value) for value in values])
            if chain is None:
                return TrajectoryRecord("UNKNOWN")
            revision = next(
                (row for row in chain if row.append_request_id == append_request_id), None
            )
            if revision is not None:
                if (
                    revision.content_digest == digest and revision.generation == expected_generation
                    and revision.predecessor_id == expected_predecessor_id
                ):
                    return TrajectoryRecord("REPLAYED", revision, revision.trajectory_id)
                return TrajectoryRecord("CONFLICT", current_id=revision.trajectory_id)
            foreign_replay = (await connection.execute(select(
                human_trajectory_revisions.c.trajectory_id
            ).where(
                human_trajectory_revisions.c.append_request_id == append_request_id
            ))).scalar_one_or_none()
            if foreign_replay is not None:
                return TrajectoryRecord("CONFLICT", current_id=foreign_replay)
            head = None if not chain else chain[-1]
            current_id = None if head is None else head.trajectory_id
            current_generation = 0 if head is None else head.generation
            if (
                expected_generation != current_generation + 1
                or expected_predecessor_id != current_id
            ):
                return TrajectoryRecord("STALE", current_id=current_id)
            values = {
                "trajectory_id": uuid4(), "append_request_id": append_request_id,
                "work_id_ref": work_id, "generation": expected_generation,
                "predecessor_id": expected_predecessor_id, "source_kind": source_kind.value,
                "source_ref": source_ref, "source_revision": source_revision,
                "content_digest": digest, "trajectory_data": data.canonical(),
            }
            inserted = (await connection.execute(insert(human_trajectory_revisions).values(
                values
            ).returning(*_COLUMNS))).one()
            revision = _row(tuple(inserted))
            return TrajectoryRecord("APPLIED", revision, revision.trajectory_id)
