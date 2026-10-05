"""Durable, inert investigation and validation task-run state."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self, cast
from uuid import UUID, uuid5

from pydantic import Field, JsonValue, model_validator
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .canonical_work import CanonicalWorkRepository, canonical_revision
from .contracts import ApiVersion, ClosedModel
from .messages import RuntimeCurrentness
from .run import RunReceipt
from .state import metadata, work_handles

REQUEST_NAMESPACE = UUID("286bcc60-8887-5b76-97c1-19c18484df74")
REQUEST_OPERATION_NAMESPACE = UUID("8a598960-f8b0-57f2-95ba-f86fc24c436c")

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
    Column("authorization_ref", Text),
    Column("send_authority_ref", Text),
    Column("content_digest", Text, nullable=False),
    Column(
        "terminal_result_id",
        PGUUID(as_uuid=True),
        ForeignKey(
            "task_run_results.result_id",
            name="fk_task_run_terminal_result",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    ),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    CheckConstraint("observed_revision <> ''", name="ck_task_run_request_revision"),
    CheckConstraint(
        "task_kind IN ('INVESTIGATION', 'VALIDATION', 'IMPLEMENTATION')",
        name="ck_task_run_request_kind",
    ),
    CheckConstraint(
        "(task_kind = 'IMPLEMENTATION' AND authorization_ref IS NOT NULL "
        "AND send_authority_ref IS NOT NULL) OR "
        "(task_kind <> 'IMPLEMENTATION' AND authorization_ref IS NULL "
        "AND send_authority_ref IS NULL)",
        name="ck_task_run_request_authority",
    ),
    CheckConstraint(
        "continuation IN ('START', 'CONTINUE', 'TAKEOVER')",
        name="ck_task_run_request_continuation",
    ),
    CheckConstraint("candidate_ref IS NULL OR candidate_ref <> ''", name="ck_task_run_candidate_ref"),
    CheckConstraint("objective <> ''", name="ck_task_run_objective"),
    CheckConstraint("length(content_digest) = 64", name="ck_task_run_request_digest"),
)

task_run_executions = Table(
    "task_run_executions",
    metadata,
    Column(
        "request_id",
        PGUUID(as_uuid=True),
        ForeignKey(task_run_requests.c.request_id, ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("run_id", PGUUID(as_uuid=True), primary_key=True),
    Column("bound_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    UniqueConstraint("run_id", name="uq_task_run_execution_run"),
)

task_run_results = Table(
    "task_run_results",
    metadata,
    Column("result_id", PGUUID(as_uuid=True), primary_key=True),
    Column("request_id", PGUUID(as_uuid=True), nullable=False),
    Column("run_id", PGUUID(as_uuid=True), nullable=False),
    Column("outcome", Text, nullable=False),
    Column("summary", Text, nullable=False),
    Column("evidence_refs", JSONB, nullable=False),
    Column("content_digest", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    ForeignKeyConstraint(
        ("request_id", "run_id"),
        ("task_run_executions.request_id", "task_run_executions.run_id"),
        ondelete="RESTRICT",
    ),
    CheckConstraint("outcome <> ''", name="ck_task_run_result_outcome"),
    CheckConstraint("summary <> ''", name="ck_task_run_result_summary"),
    CheckConstraint("length(content_digest) = 64", name="ck_task_run_result_digest"),
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


class ImplementationTaskRequest(ClosedModel):
    """Server-derived implementation intent; never accepted as a public caller payload."""

    api_version: ApiVersion = "1"
    execution_work_id: UUID
    observed_revision: str = Field(min_length=1, max_length=512)
    task_kind: Literal["IMPLEMENTATION"] = "IMPLEMENTATION"
    objective: str = Field(min_length=1, max_length=8000)
    result_contract: dict[str, JsonValue]
    authorization_ref: str = Field(min_length=1, max_length=1024)
    send_authority_ref: str = Field(min_length=1, max_length=1024)
    continuation: Literal["START"] = "START"
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
    task_kind: Literal["INVESTIGATION", "VALIDATION", "IMPLEMENTATION"]
    objective: str
    result_contract: dict[str, JsonValue]
    authorization_ref: str | None = None
    send_authority_ref: str | None = None
    continuation: Literal["START", "CONTINUE", "TAKEOVER"]
    candidate_ref: str | None = None
    terminal_result_id: UUID | None = None


class AgentTaskResult(ClosedModel):
    api_version: ApiVersion
    outcome: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=8000)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def bounded_evidence(self) -> Self:
        if any(not value or len(value) > 2048 for value in self.evidence_refs):
            raise ValueError("evidence references must contain 1..2048 characters")
        return self


class TaskRunExecution(ClosedModel):
    request_id: UUID
    run_id: UUID


class TaskRunBindResult(ClosedModel):
    status: Literal["ok", "denied", "conflict", "unknown"]
    execution: TaskRunExecution | None = None
    reason: Literal[
        "request_not_found",
        "execution_work_mismatch",
        "continuation_not_bound",
        "execution_already_bound",
        "run_already_bound",
        "state_unavailable",
    ] | None = None

    @model_validator(mode="after")
    def exact_shape(self) -> Self:
        if self.status == "ok" and (self.execution is None or self.reason is not None):
            raise ValueError("successful bind requires only the execution")
        if self.status != "ok" and (self.execution is not None or self.reason is None):
            raise ValueError("failed bind requires only its reason")
        return self


class TaskRunResult(ClosedModel):
    result_id: UUID
    request_id: UUID
    run_id: UUID
    outcome: str
    summary: str
    evidence_refs: tuple[str, ...]


class TaskRunResultResult(ClosedModel):
    status: Literal["ok", "stale", "denied", "conflict", "unknown"]
    result: TaskRunResult | None = None
    terminal: bool = False
    reason: Literal[
        "request_not_found",
        "execution_not_bound",
        "result_identity_conflict",
        "terminal_result_conflict",
        "run_superseded",
        "runtime_currentness_unavailable",
        "state_unavailable",
    ] | None = None


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
        "no_current_grant",
        "operation_not_granted",
        "requester_run_superseded",
        "runtime_currentness_unavailable",
        "state_unavailable",
    ] | None = None

    @model_validator(mode="after")
    def exact_shape(self) -> Self:
        if self.status == "ok" and (self.request is None or self.reason is not None):
            raise ValueError("successful request result requires only the request")
        if self.status != "ok" and (self.request is not None or self.reason is None):
            raise ValueError("failed request result requires only its reason")
        return self


_REQUEST_COLUMNS = tuple(task_run_requests.c)


def _digest(
    requester_work_id: UUID, request: AgentTaskRequest | ImplementationTaskRequest
) -> str:
    value = {
        "requester_work_id": str(requester_work_id),
        "request": request.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def task_request_operation_id(
    requester_work_id: UUID, request: AgentTaskRequest,
) -> UUID:
    """Derive stable server-owned replay identity from the complete semantic request."""
    return uuid5(REQUEST_OPERATION_NAMESPACE, _digest(requester_work_id, request))


async def _verified_request(
    connection: AsyncConnection, row: RowMapping
) -> TaskRunRequest:
    requester_work_id = cast(UUID, row["requester_work_id"])
    if row["task_kind"] == "IMPLEMENTATION":
        payload = ImplementationTaskRequest(
            api_version="1",
            execution_work_id=cast(UUID, row["execution_work_id"]),
            observed_revision=cast(str, row["observed_revision"]),
            candidate_ref=cast(str | None, row["candidate_ref"]),
            objective=cast(str, row["objective"]),
            result_contract=cast(dict[str, JsonValue], row["result_contract"]),
            authorization_ref=cast(str, row["authorization_ref"]),
            send_authority_ref=cast(str, row["send_authority_ref"]),
        )
    else:
        if row["authorization_ref"] is not None or row["send_authority_ref"] is not None:
            raise ValueError("stored generic task-run request has authority references")
        payload = AgentTaskRequest(
            api_version="1",
            execution_work_id=cast(UUID, row["execution_work_id"]),
            observed_revision=cast(str, row["observed_revision"]),
            candidate_ref=cast(str | None, row["candidate_ref"]),
            objective=cast(str, row["objective"]),
            result_contract=cast(dict[str, JsonValue], row["result_contract"]),
            task_kind=cast(Literal["INVESTIGATION", "VALIDATION"], row["task_kind"]),
            continuation=cast(
                Literal["START", "CONTINUE", "TAKEOVER"], row["continuation"]
            ),
        )
    if row["content_digest"] != _digest(requester_work_id, payload):
        raise ValueError("stored task-run request digest mismatch")
    request_id = cast(UUID, row["request_id"])
    terminal_result_id = cast(UUID | None, row["terminal_result_id"])
    if terminal_result_id is not None:
        terminal_owner = await connection.scalar(
            select(task_run_results.c.request_id).where(
                task_run_results.c.result_id == terminal_result_id
            )
        )
        if terminal_owner != request_id:
            raise ValueError("stored task-run terminal result owner mismatch")
    return TaskRunRequest(
        request_id=request_id,
        requester_work_id=requester_work_id,
        terminal_result_id=terminal_result_id,
        **payload.model_dump(mode="python", exclude={"api_version"}),
    )


def _result_digest(
    request_id: UUID, run_id: UUID, result: AgentTaskResult
) -> str:
    value = {
        "request_id": str(request_id),
        "run_id": str(run_id),
        "result": result.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _result_view(row: RowMapping) -> TaskRunResult:
    request_id = cast(UUID, row["request_id"])
    run_id = cast(UUID, row["run_id"])
    payload = AgentTaskResult(
        api_version="1",
        outcome=cast(str, row["outcome"]),
        summary=cast(str, row["summary"]),
        evidence_refs=tuple(cast(list[str], row["evidence_refs"])),
    )
    if row["content_digest"] != _result_digest(request_id, run_id, payload):
        raise ValueError("stored task-run result digest mismatch")
    return TaskRunResult(
        result_id=cast(UUID, row["result_id"]),
        request_id=request_id,
        run_id=run_id,
        outcome=payload.outcome,
        summary=payload.summary,
        evidence_refs=payload.evidence_refs,
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
        try:
            async with self.engine.begin() as connection:
                return await self.request_in_transaction(
                    connection, requester_work_id, operation_id, request
                )
        except (SQLAlchemyError, TypeError, ValueError):
            return TaskRunRequestResult(status="unknown", reason="state_unavailable")

    async def request_in_transaction(
        self,
        connection: AsyncConnection,
        requester_work_id: UUID,
        operation_id: UUID,
        request: AgentTaskRequest | ImplementationTaskRequest,
    ) -> TaskRunRequestResult:
        """Admit one request inside a caller-owned atomic currentness transaction."""
        digest = _digest(requester_work_id, request)
        request_id = uuid5(REQUEST_NAMESPACE, str(operation_id))
        replay = (await connection.execute(select(task_run_requests).where(
                    task_run_requests.c.operation_id == operation_id
                ).with_for_update())).mappings().one_or_none()
        if replay is not None:
            verified = await _verified_request(connection, replay)
            if replay["content_digest"] != digest:
                return TaskRunRequestResult(
                    status="conflict", reason="operation_identity_conflict"
                )
            return TaskRunRequestResult(status="ok", request=verified)
        requester = (await connection.execute(select(work_handles.c.id).where(
            work_handles.c.id == requester_work_id
        ).with_for_update(read=True))).scalar_one_or_none()
        if requester is None:
            return TaskRunRequestResult(status="denied", reason="requester_work_not_found")
        execution = await self.works.get_locked(connection, request.execution_work_id)
        if execution is None:
            return TaskRunRequestResult(status="denied", reason="execution_work_not_found")
        if canonical_revision(execution.work_id, execution.row_version) != request.observed_revision:
            return TaskRunRequestResult(status="stale", reason="source_revision_changed")
        if request.continuation != "START":
            return TaskRunRequestResult(status="denied", reason="continuation_not_bound")
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
            return TaskRunRequestResult(
                status="ok", request=await _verified_request(connection, inserted)
            )
        replay = (await connection.execute(select(task_run_requests).where(
            task_run_requests.c.operation_id == operation_id
        ))).mappings().one_or_none()
        if replay is None:
            return TaskRunRequestResult(status="conflict", reason="operation_identity_conflict")
        verified = await _verified_request(connection, replay)
        if replay["content_digest"] != digest:
            return TaskRunRequestResult(status="conflict", reason="operation_identity_conflict")
        return TaskRunRequestResult(status="ok", request=verified)

    async def get_operation_in_transaction(
        self, connection: AsyncConnection, operation_id: UUID
    ) -> TaskRunRequestResult:
        """Lock and verify an admitted operation inside its caller-owned transaction."""
        row = (await connection.execute(
            select(task_run_requests).where(
                task_run_requests.c.operation_id == operation_id
            ).with_for_update()
        )).mappings().one_or_none()
        if row is None:
            return TaskRunRequestResult(status="denied", reason="request_not_found")
        return TaskRunRequestResult(
            status="ok", request=await _verified_request(connection, row)
        )

    async def get(self, request_id: UUID) -> TaskRunRequestResult:
        try:
            async with self.engine.connect() as connection:
                row = (await connection.execute(select(task_run_requests).where(
                    task_run_requests.c.request_id == request_id
                ))).mappings().one_or_none()
                if row is None:
                    return TaskRunRequestResult(status="denied", reason="request_not_found")
                return TaskRunRequestResult(
                    status="ok", request=await _verified_request(connection, row)
                )
        except (SQLAlchemyError, TypeError, ValueError):
            return TaskRunRequestResult(status="unknown", reason="state_unavailable")

    async def bind_start(
        self, request_id: UUID, receipt: RunReceipt
    ) -> TaskRunBindResult:
        try:
            async with self.engine.begin() as connection:
                request = (await connection.execute(
                    select(task_run_requests).where(
                        task_run_requests.c.request_id == request_id
                    ).with_for_update()
                )).mappings().one_or_none()
                if request is None:
                    return TaskRunBindResult(status="denied", reason="request_not_found")
                verified = await _verified_request(connection, request)
                if verified.execution_work_id != receipt.active_work_id:
                    return TaskRunBindResult(
                        status="denied", reason="execution_work_mismatch"
                    )
                if verified.continuation != "START":
                    return TaskRunBindResult(
                        status="denied", reason="continuation_not_bound"
                    )
                current = (await connection.execute(
                    select(task_run_executions).where(
                        task_run_executions.c.request_id == request_id
                    )
                )).mappings().one_or_none()
                if current is not None:
                    if current["run_id"] != receipt.run_id:
                        return TaskRunBindResult(
                            status="denied", reason="execution_already_bound"
                        )
                    return TaskRunBindResult(
                        status="ok",
                        execution=TaskRunExecution(
                            request_id=request_id, run_id=receipt.run_id
                        ),
                    )
                inserted = (await connection.execute(
                    insert(task_run_executions).values(
                        request_id=request_id,
                        run_id=receipt.run_id,
                    ).on_conflict_do_nothing().returning(task_run_executions.c.run_id)
                )).scalar_one_or_none()
                if inserted is None:
                    return TaskRunBindResult(status="conflict", reason="run_already_bound")
                return TaskRunBindResult(
                    status="ok",
                    execution=TaskRunExecution(
                        request_id=request_id, run_id=receipt.run_id
                    ),
                )
        except (SQLAlchemyError, TypeError, ValueError):
            return TaskRunBindResult(status="unknown", reason="state_unavailable")

    async def submit_result(
        self,
        request_id: UUID,
        result_id: UUID,
        runtime: RuntimeCurrentness,
        result: AgentTaskResult,
    ) -> TaskRunResultResult:
        try:
            run_id = UUID(runtime.generation)
            digest = _result_digest(request_id, run_id, result)
            async with self.engine.begin() as connection:
                request = (await connection.execute(
                    select(task_run_requests).where(
                        task_run_requests.c.request_id == request_id
                    ).with_for_update()
                )).mappings().one_or_none()
                if request is None:
                    return TaskRunResultResult(status="denied", reason="request_not_found")
                verified = await _verified_request(connection, request)
                execution = (await connection.execute(
                    select(task_run_executions).where(
                        task_run_executions.c.request_id == request_id,
                        task_run_executions.c.run_id == run_id,
                    )
                )).mappings().one_or_none()
                if execution is None:
                    return TaskRunResultResult(
                        status="denied", reason="execution_not_bound"
                    )
                execution_count = await connection.scalar(
                    select(func.count()).select_from(task_run_executions).where(
                        task_run_executions.c.request_id == request_id
                    )
                )
                if execution_count != 1:
                    return TaskRunResultResult(
                        status="unknown", reason="state_unavailable"
                    )
                existing = (await connection.execute(
                    select(task_run_results).where(
                        task_run_results.c.result_id == result_id
                    )
                )).mappings().one_or_none()
                if existing is not None and existing["content_digest"] != digest:
                    return TaskRunResultResult(
                        status="conflict", reason="result_identity_conflict"
                    )
                if existing is None:
                    existing = (await connection.execute(
                        insert(task_run_results).values(
                            result_id=result_id,
                            request_id=request_id,
                            run_id=run_id,
                            content_digest=digest,
                            **result.model_dump(mode="json", exclude={"api_version"}),
                        ).returning(*tuple(task_run_results.c))
                    )).mappings().one()
                view = _result_view(existing)
                if runtime.current_generation is None:
                    return TaskRunResultResult(
                        status="unknown",
                        result=view,
                        reason="runtime_currentness_unavailable",
                    )
                if runtime.current_generation != runtime.generation:
                    return TaskRunResultResult(
                        status="stale", result=view, reason="run_superseded"
                    )
                terminal = verified.terminal_result_id
                if terminal not in {None, result_id}:
                    return TaskRunResultResult(
                        status="conflict",
                        result=view,
                        reason="terminal_result_conflict",
                    )
                if terminal is None:
                    await connection.execute(
                        update(task_run_requests).where(
                            task_run_requests.c.request_id == request_id
                        ).values(terminal_result_id=result_id)
                    )
                return TaskRunResultResult(status="ok", result=view, terminal=True)
        except (SQLAlchemyError, TypeError, ValueError):
            return TaskRunResultResult(status="unknown", reason="state_unavailable")
