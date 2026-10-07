"""Durable owner checkpoints for canonical task control."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .canonical_relations import work_dependencies, work_parents
from .canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_metadata,
    canonical_revision,
)
from .contracts import ClosedModel
from .durable_capture import FindingCapture
from .grants import PrincipalContext, WorkGrant

task_control_checkpoints = Table(
    "task_control_checkpoints", canonical_metadata,
    Column("checkpoint_id", PGUUID(as_uuid=True), primary_key=True),
    Column("operation_id", PGUUID(as_uuid=True), nullable=False, unique=True),
    Column("work_id", PGUUID(as_uuid=True),
           ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("observed_work_revision", Text, nullable=False),
    Column("control_basis_digest", Text, nullable=False),
    Column("capsule", JSONB, nullable=False),
    Column("content_digest", Text, nullable=False),
    Column("request_digest", Text, nullable=False),
    Column("principal_key", Text, nullable=False),
    Column("grant_id", PGUUID(as_uuid=True), nullable=False),
    Column("grant_version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("work_id", "generation", name="uq_task_control_work_generation"),
    CheckConstraint("generation >= 1", name="ck_task_control_generation"),
    CheckConstraint(
        "length(control_basis_digest) = 64 AND length(content_digest) = 64 "
        "AND length(request_digest) = 64",
        name="ck_task_control_digests",
    ),
    CheckConstraint(
        "observed_work_revision <> '' AND principal_key <> ''",
        name="ck_task_control_nonempty",
    ),
)


class AttributableCorrection(ClosedModel):
    summary: str = Field(min_length=1, max_length=4000)
    source_ref: str = Field(min_length=1, max_length=2048)
    finding_capture: FindingCapture | None = None


class SuspendedReturn(ClosedModel):
    obligation: str | None = Field(default=None, min_length=1, max_length=4000)
    work_id: UUID | None = None
    return_condition: str = Field(min_length=1, max_length=4000)
    source_ref: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def has_obligation(self) -> SuspendedReturn:
        if self.obligation is None and self.work_id is None:
            raise ValueError("suspended return requires an obligation or WorkId")
        return self


class DurableControlCapsule(ClosedModel):
    schema_version: Literal[1] = 1
    objective: str = Field(min_length=1, max_length=4000)
    completion_condition: str = Field(min_length=1, max_length=4000)
    success_proof: str = Field(min_length=1, max_length=4000)
    target_refs: tuple[str, ...] = Field(default=(), max_length=64)
    intended_effect_class: str = Field(min_length=1, max_length=500)
    progress_summary: str = Field(min_length=1, max_length=4000)
    progress_evidence_ref: str = Field(min_length=1, max_length=2048)
    current_unknowns: tuple[str, ...] = Field(default=(), max_length=64)
    applicable_corrections: tuple[AttributableCorrection, ...] = Field(
        default=(), max_length=64,
    )
    suspended_return: SuspendedReturn | None = None
    do_not_retry_refs: tuple[str, ...] = Field(default=(), max_length=64)
    failed_route_refs: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def bounded(self) -> DurableControlCapsule:
        values = (
            *self.target_refs, *self.current_unknowns,
            *self.do_not_retry_refs, *self.failed_route_refs,
        )
        if any(not value or len(value) > 2048 for value in values):
            raise ValueError("capsule references must be nonempty and bounded")
        if len(_canonical(self.model_dump(mode="json"))) > 64 * 1024:
            raise ValueError("control capsule exceeds 64 KiB")
        return self


class TaskControlCheckpoint(ClosedModel):
    checkpoint_id: UUID
    work_id: UUID
    generation: int = Field(ge=1)
    observed_work_revision: str
    control_basis_digest: str
    content_digest: str
    capsule: DurableControlCapsule
    created_at: datetime


class TaskControlCheckpointReceipt(ClosedModel):
    operation_id: UUID
    checkpoint_id: UUID
    work_id: UUID
    generation: int
    observed_work_revision: str
    control_basis_digest: str
    content_digest: str
    principal_key: str
    grant_id: UUID
    grant_version: int
    created_at: datetime


class TaskControlReadResult(ClosedModel):
    status: Literal["ok", "stale", "denied", "unknown"]
    currentness: Literal["CURRENT", "STALE", "UNKNOWN"]
    reason: str
    checkpoint: TaskControlCheckpoint | None = None
    current_work_revision: str | None = None


class TaskControlCheckpointResult(ClosedModel):
    status: Literal["ok", "stale", "denied", "conflict", "unknown"]
    currentness: Literal["CURRENT", "STALE", "UNKNOWN"]
    reason: str
    checkpoint: TaskControlCheckpoint | None = None
    receipt: TaskControlCheckpointReceipt | None = None
    current_work_revision: str | None = None


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


async def _basis(
    connection: AsyncConnection, work: CurrentWork,
) -> str | None:
    if work.canonical_root in {None, "UNKNOWN"}:
        return None
    parent = await connection.scalar(select(work_parents.c.parent_work_id).where(
        work_parents.c.child_work_id == work.work_id
    ))
    dependencies = tuple((await connection.scalars(select(
        work_dependencies.c.depends_on_work_id
    ).where(work_dependencies.c.work_id == work.work_id).order_by(
        work_dependencies.c.depends_on_work_id
    ).limit(257))).all())
    if len(dependencies) > 256:
        return None
    value = {
        "schema_version": 1,
        "work_id": str(work.work_id),
        "canonical_root": work.canonical_root,
        "parent_work_id": None if parent is None else str(parent),
        "dependency_work_ids": [str(value) for value in dependencies],
        "control": {
            field: getattr(work, field) for field in (
                "title", "completed", "assignee", "priority", "work_type",
                "lifecycle_state", "review_next_action", "owner_key", "wait_kind",
                "unblock_condition", "next_due", "next_action_class", "next_action_ref",
            )
        },
    }
    return _digest(value)


class TaskControlState:
    def __init__(self, engine: AsyncEngine, works: CanonicalWorkRepository):
        self.engine, self.works = engine, works

    @staticmethod
    def _checkpoint(row: object) -> TaskControlCheckpoint:
        values = cast(dict[str, object], row)
        return TaskControlCheckpoint(
            checkpoint_id=cast(UUID, values["checkpoint_id"]),
            work_id=cast(UUID, values["work_id"]),
            generation=cast(int, values["generation"]),
            observed_work_revision=cast(str, values["observed_work_revision"]),
            control_basis_digest=cast(str, values["control_basis_digest"]),
            content_digest=cast(str, values["content_digest"]),
            capsule=DurableControlCapsule.model_validate(values["capsule"]),
            created_at=cast(datetime, values["created_at"]),
        )

    async def read(self, work_id: UUID) -> TaskControlReadResult:
        try:
            async with self.engine.connect() as connection:
                return await self.read_in(connection, work_id)
        except (SQLAlchemyError, KeyError, TypeError, ValueError):
            return TaskControlReadResult(
                status="unknown", currentness="UNKNOWN", reason="state_unavailable",
            )

    async def read_in(
        self, connection: AsyncConnection, work_id: UUID,
    ) -> TaskControlReadResult:
        """Read checkpoint currentness in a caller-owned coherent snapshot."""
        work = await self.works.get_in(connection, work_id)
        if work is None:
            return TaskControlReadResult(
                status="unknown", currentness="UNKNOWN", reason="work_not_found",
            )
        revision = canonical_revision(work_id, work.row_version)
        row = (await connection.execute(select(task_control_checkpoints).where(
            task_control_checkpoints.c.work_id == work_id
        ).order_by(task_control_checkpoints.c.generation.desc()).limit(1))).mappings().one_or_none()
        if row is None:
            return TaskControlReadResult(
                status="unknown", currentness="UNKNOWN", reason="missing_checkpoint",
                current_work_revision=revision,
            )
        checkpoint = self._checkpoint(dict(row))
        basis = await _basis(connection, work)
        if basis is None:
            return TaskControlReadResult(
                status="unknown", currentness="UNKNOWN", reason="basis_unavailable",
                checkpoint=checkpoint, current_work_revision=revision,
            )
        current = basis == checkpoint.control_basis_digest
        return TaskControlReadResult(
            status="ok" if current else "stale",
            currentness="CURRENT" if current else "STALE",
            reason="checkpoint_current" if current else "control_basis_changed",
            checkpoint=checkpoint, current_work_revision=revision,
        )

    async def checkpoint(
        self, principal: PrincipalContext, grant: WorkGrant, operation_id: UUID,
        work_id: UUID, observed_work_revision: str,
        expected_checkpoint_generation: int | None, capsule: DurableControlCapsule,
    ) -> TaskControlCheckpointResult:
        if "task_control" not in grant.operations or not grant.can_write(work_id):
            return TaskControlCheckpointResult(
                status="denied", currentness="UNKNOWN", reason="work_not_granted",
            )
        capsule_json = capsule.model_dump(mode="json")
        content_digest = _digest(capsule_json)
        request_digest = _digest({
            "principal_key": principal.key, "work_id": str(work_id),
            "observed_work_revision": observed_work_revision,
            "expected_checkpoint_generation": expected_checkpoint_generation,
            "content_digest": content_digest,
        })
        try:
            async with self.engine.begin() as connection:
                replay = (await connection.execute(select(task_control_checkpoints).where(
                    task_control_checkpoints.c.operation_id == operation_id
                ))).mappings().one_or_none()
                if replay is not None:
                    if replay["request_digest"] != request_digest:
                        return TaskControlCheckpointResult(
                            status="conflict", currentness="UNKNOWN",
                            reason="operation_identity_conflict",
                        )
                    checkpoint = self._checkpoint(dict(replay))
                    work = await self.works.get_locked(connection, work_id)
                    if work is None:
                        return TaskControlCheckpointResult(
                            status="unknown", currentness="UNKNOWN", reason="work_not_found",
                            checkpoint=checkpoint, receipt=self._receipt(dict(replay)),
                        )
                    current_revision = canonical_revision(work_id, work.row_version)
                    basis = await _basis(connection, work)
                    if basis is None:
                        return TaskControlCheckpointResult(
                            status="unknown", currentness="UNKNOWN", reason="basis_unavailable",
                            checkpoint=checkpoint, receipt=self._receipt(dict(replay)),
                            current_work_revision=current_revision,
                        )
                    current = basis == checkpoint.control_basis_digest
                    return TaskControlCheckpointResult(
                        status="ok" if current else "stale",
                        currentness="CURRENT" if current else "STALE",
                        reason="exact_replay" if current else "replayed_checkpoint_stale",
                        checkpoint=checkpoint, receipt=self._receipt(dict(replay)),
                        current_work_revision=current_revision,
                    )
                work = await self.works.get_locked(connection, work_id)
                if work is None:
                    return TaskControlCheckpointResult(
                        status="denied", currentness="UNKNOWN", reason="work_not_found",
                    )
                current_revision = canonical_revision(work_id, work.row_version)
                if current_revision != observed_work_revision:
                    return TaskControlCheckpointResult(
                        status="stale", currentness="STALE", reason="work_revision_changed",
                        current_work_revision=current_revision,
                    )
                latest = (await connection.execute(select(task_control_checkpoints).where(
                    task_control_checkpoints.c.work_id == work_id
                ).order_by(task_control_checkpoints.c.generation.desc()).limit(1).with_for_update())).mappings().one_or_none()
                actual_generation = None if latest is None else cast(int, latest["generation"])
                if actual_generation != expected_checkpoint_generation:
                    return TaskControlCheckpointResult(
                        status="stale", currentness="STALE",
                        reason="checkpoint_generation_changed",
                        current_work_revision=current_revision,
                    )
                basis = await _basis(connection, work)
                if basis is None:
                    return TaskControlCheckpointResult(
                        status="unknown", currentness="UNKNOWN", reason="basis_unavailable",
                        current_work_revision=current_revision,
                    )
                now, generation, checkpoint_id = datetime.now(UTC), (actual_generation or 0) + 1, uuid4()
                values = {
                    "checkpoint_id": checkpoint_id, "operation_id": operation_id,
                    "work_id": work_id, "generation": generation,
                    "observed_work_revision": current_revision,
                    "control_basis_digest": basis, "capsule": capsule_json,
                    "content_digest": content_digest, "request_digest": request_digest,
                    "principal_key": principal.key, "grant_id": grant.id,
                    "grant_version": grant.version, "created_at": now,
                }
                await connection.execute(insert(task_control_checkpoints).values(**values))
                checkpoint = self._checkpoint(values)
                return TaskControlCheckpointResult(
                    status="ok", currentness="CURRENT", reason="checkpoint_recorded",
                    checkpoint=checkpoint, receipt=self._receipt(values),
                    current_work_revision=current_revision,
                )
        except IntegrityError:
            return TaskControlCheckpointResult(
                status="stale", currentness="STALE", reason="concurrent_checkpoint",
            )
        except (SQLAlchemyError, KeyError, TypeError, ValueError):
            return TaskControlCheckpointResult(
                status="unknown", currentness="UNKNOWN", reason="state_unavailable",
            )

    @staticmethod
    def _receipt(values: Mapping[str, object]) -> TaskControlCheckpointReceipt:
        return TaskControlCheckpointReceipt(
            operation_id=cast(UUID, values["operation_id"]),
            checkpoint_id=cast(UUID, values["checkpoint_id"]),
            work_id=cast(UUID, values["work_id"]), generation=cast(int, values["generation"]),
            observed_work_revision=cast(str, values["observed_work_revision"]),
            control_basis_digest=cast(str, values["control_basis_digest"]),
            content_digest=cast(str, values["content_digest"]),
            principal_key=cast(str, values["principal_key"]),
            grant_id=cast(UUID, values["grant_id"]),
            grant_version=cast(int, values["grant_version"]),
            created_at=cast(datetime, values["created_at"]),
        )
