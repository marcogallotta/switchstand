"""Durable, inert investigation and validation task-run state."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self, cast
from uuid import UUID, uuid5

from pydantic import Field, JsonValue, model_validator
from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Table, Text, func, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from .canonical_work import CanonicalWorkRepository, canonical_revision
from .contracts import ApiVersion, ClosedModel
from .state import metadata, work_handles

REQUEST_NAMESPACE = UUID("286bcc60-8887-5b76-97c1-19c18484df74")

task_run_requests = Table(
    "task_run_requests",
    metadata,
    Column("request_id", PGUUID(as_uuid=True), primary_key=True),
    Column("operation_id", PGUUID(as_uuid=True), nullable=False, unique=True),
    Column(
        "requester_work_id",
        PGUUID(as_uuid=True),
        ForeignKey(work_handles.c.id, ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "execution_work_id",
        PGUUID(as_uuid=True),
        ForeignKey(work_handles.c.id, ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("observed_revision", Text, nullable=False),
    Column("task_kind", Text, nullable=False),
    Column("continuation", Text, nullable=False),
    Column("candidate_ref", Text),
    Column("objective", Text, nullable=False),
    Column("result_contract", JSONB, nullable=False),
    Column("content_digest", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    CheckConstraint("observed_revision <> ''", name="ck_task_run_request_revision"),
    CheckConstraint("task_kind IN ('INVESTIGATION', 'VALIDATION')", name="ck_task_run_request_kind"),
    CheckConstraint(
        "continuation IN ('START', 'CONTINUE', 'TAKEOVER')",
        name="ck_task_run_request_continuation",
    ),
    CheckConstraint("candidate_ref IS NULL OR candidate_ref <> ''", name="ck_task_run_candidate_ref"),
    CheckConstraint("objective <> ''", name="ck_task_run_objective"),
    CheckConstraint("length(content_digest) = 64", name="ck_task_run_request_digest"),
)


class AgentTaskRequest(ClosedModel):
    """Public intent; operation identity and requester route remain server-owned."""

    api_version: ApiVersion
    execution_work_id: UUID
    observed_revision: str = Field(min_length=1, max_length=512)
    task_kind: Literal["INVESTIGATION", "VALIDATION"]
    objective: str = Field(min_length=1, max_length=8000)
    result_contract: dict[str, JsonValue]
    continuation: Literal["START", "CONTINUE", "TAKEOVER"] = "START"
    candidate_ref: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def bounded_contract(self) -> Self:
        encoded = json.dumps(
            self.result_contract, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        if len(encoded) > 16_384:
            raise ValueError("result contract exceeds 16384 bytes")
        return self


class TaskRunRequest(ClosedModel):
    request_id: UUID
    requester_work_id: UUID
    execution_work_id: UUID
    observed_revision: str
    task_kind: Literal["INVESTIGATION", "VALIDATION"]
    objective: str
    result_contract: dict[str, JsonValue]
    continuation: Literal["START", "CONTINUE", "TAKEOVER"]
    candidate_ref: str | None = None


class TaskRunRequestResult(ClosedModel):
    status: Literal["ok", "stale", "denied", "conflict", "unknown"]
    request: TaskRunRequest | None = None
    reason: Literal[
        "request_not_found",
        "requester_work_not_found",
        "execution_work_not_found",
        "source_revision_changed",
        "continuation_not_bound",
        "operation_identity_conflict",
        "state_unavailable",
    ] | None = None


_REQUEST_COLUMNS = tuple(task_run_requests.c)


def _digest(requester_work_id: UUID, request: AgentTaskRequest) -> str:
    value = {
        "requester_work_id": str(requester_work_id),
        "request": request.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _view(row: RowMapping) -> TaskRunRequest:
    return TaskRunRequest(
        request_id=cast(UUID, row["request_id"]),
        requester_work_id=cast(UUID, row["requester_work_id"]),
        execution_work_id=cast(UUID, row["execution_work_id"]),
        observed_revision=cast(str, row["observed_revision"]),
        task_kind=cast(Literal["INVESTIGATION", "VALIDATION"], row["task_kind"]),
        continuation=cast(Literal["START", "CONTINUE", "TAKEOVER"], row["continuation"]),
        candidate_ref=cast(str | None, row["candidate_ref"]),
        objective=cast(str, row["objective"]),
        result_contract=cast(dict[str, JsonValue], row["result_contract"]),
    )


class TaskRunState:
    """Canonical persistence owner; transport and process launch remain outside it."""

    def __init__(self, engine: AsyncEngine, works: CanonicalWorkRepository):
        self.engine = engine
        self.works = works

    async def request(
        self,
        requester_work_id: UUID,
        operation_id: UUID,
        request: AgentTaskRequest,
    ) -> TaskRunRequestResult:
        digest = _digest(requester_work_id, request)
        request_id = uuid5(REQUEST_NAMESPACE, str(operation_id))
        try:
            async with self.engine.begin() as connection:
                replay = (await connection.execute(select(task_run_requests).where(
                    task_run_requests.c.operation_id == operation_id
                ).with_for_update())).mappings().one_or_none()
                if replay is not None:
                    if replay["content_digest"] != digest:
                        return TaskRunRequestResult(
                            status="conflict", reason="operation_identity_conflict"
                        )
                    return TaskRunRequestResult(status="ok", request=_view(replay))
                requester = (await connection.execute(select(work_handles.c.id).where(
                    work_handles.c.id == requester_work_id
                ).with_for_update(read=True))).scalar_one_or_none()
                if requester is None:
                    return TaskRunRequestResult(
                        status="denied", reason="requester_work_not_found"
                    )
                execution = await self.works.get_locked(connection, request.execution_work_id)
                if execution is None:
                    return TaskRunRequestResult(
                        status="denied", reason="execution_work_not_found"
                    )
                if canonical_revision(execution.work_id, execution.row_version) != (
                    request.observed_revision
                ):
                    return TaskRunRequestResult(
                        status="stale", reason="source_revision_changed"
                    )
                if request.continuation != "START":
                    return TaskRunRequestResult(
                        status="denied", reason="continuation_not_bound"
                    )
                values = {
                    "request_id": request_id,
                    "operation_id": operation_id,
                    "requester_work_id": requester_work_id,
                    "content_digest": digest,
                    **request.model_dump(mode="json", exclude={"api_version"}),
                }
                inserted = (await connection.execute(
                    insert(task_run_requests).values(values).on_conflict_do_nothing().returning(
                        *_REQUEST_COLUMNS
                    )
                )).mappings().one_or_none()
                if inserted is not None:
                    return TaskRunRequestResult(status="ok", request=_view(inserted))
                replay = (await connection.execute(select(task_run_requests).where(
                    task_run_requests.c.operation_id == operation_id
                ))).mappings().one_or_none()
                if replay is None or replay["content_digest"] != digest:
                    return TaskRunRequestResult(
                        status="conflict", reason="operation_identity_conflict"
                    )
                return TaskRunRequestResult(status="ok", request=_view(replay))
        except (SQLAlchemyError, TypeError, ValueError):
            return TaskRunRequestResult(status="unknown", reason="state_unavailable")
