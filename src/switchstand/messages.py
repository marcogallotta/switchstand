"""Durable message envelopes and per-recipient delivery state."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal, Self
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field, JsonValue, model_validator
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    and_,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from .contracts import ApiVersion, ClosedModel
from .grant_state import GrantState
from .grants import PrincipalContext, WorkGrant
from .state import metadata

DELIVERY_NAMESPACE = UUID("8b7eedf9-138d-4a5e-9060-7c208138402d")
PROJECTION_NAMESPACE = UUID("5cf70ae7-4d39-4398-82d6-967d2cb6e3bc")

messages = Table(
    "messages",
    metadata,
    Column("sender_work_id", PGUUID(as_uuid=True), primary_key=True),
    Column("message_id", PGUUID(as_uuid=True), primary_key=True),
    Column("route_ref", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("digest", Text, nullable=False),
    Column("in_reply_to_delivery_id", PGUUID(as_uuid=True), nullable=True, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

message_deliveries = Table(
    "message_deliveries",
    metadata,
    Column("delivery_id", PGUUID(as_uuid=True), primary_key=True),
    Column("sender_work_id", PGUUID(as_uuid=True), nullable=False),
    Column("message_id", PGUUID(as_uuid=True), nullable=False),
    Column("recipient_work_id", PGUUID(as_uuid=True), nullable=False),
    Column("state", Text, nullable=False, server_default="AVAILABLE"),
    Column("recipient_grant_version", Integer, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=True),
    Column("receiving_generation", Text, nullable=True),
    Column("dispositioned_at", DateTime(timezone=True), nullable=True),
    Column("disposition_digest", Text, nullable=True),
    Column("evidence", JSONB, nullable=True),
    ForeignKeyConstraint(
        ["sender_work_id", "message_id"],
        ["messages.sender_work_id", "messages.message_id"],
        ondelete="RESTRICT",
    ),
    UniqueConstraint("sender_work_id", "message_id", "recipient_work_id"),
    CheckConstraint("state IN ('AVAILABLE', 'RECEIVED', 'DISPOSITIONED')"),
)

message_projection = Table(
    "message_projection",
    metadata,
    Column("projection_id", PGUUID(as_uuid=True), primary_key=True),
    Column("sender_work_id", PGUUID(as_uuid=True), nullable=False),
    Column("message_id", PGUUID(as_uuid=True), nullable=False),
    Column("operation_id", PGUUID(as_uuid=True), nullable=False, unique=True),
    Column("provider", Text, nullable=False),
    Column("target", Text, nullable=False),
    Column("state", Text, nullable=False, server_default="PENDING"),
    Column("receipt", JSONB, nullable=True),
    ForeignKeyConstraint(
        ["sender_work_id", "message_id"],
        ["messages.sender_work_id", "messages.message_id"],
        ondelete="RESTRICT",
    ),
    UniqueConstraint("sender_work_id", "message_id", "provider", "target"),
    CheckConstraint("state IN ('PENDING', 'CONFIRMED', 'UNKNOWN')"),
)


class MessageRoute(ClosedModel):
    recipient_work_id: UUID
    recipient_grant_version: int = Field(ge=1)
    projection_provider: Literal["asana"]
    projection_target: str = Field(min_length=1, pattern=r"^[0-9]+$")


class MessageSubmitRequest(ClosedModel):
    api_version: ApiVersion
    message_id: UUID
    grant_version: int = Field(ge=1)
    route_ref: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    kind: Literal["request", "result"]
    payload: JsonValue
    in_reply_to_delivery_id: UUID | None = None

    @model_validator(mode="after")
    def correlated_result(self) -> Self:
        if (self.kind == "result") != (self.in_reply_to_delivery_id is not None):
            raise ValueError("only results carry an exact reply delivery correlation")
        return self


class PendingMessage(ClosedModel):
    delivery_id: UUID
    message_id: UUID
    sender_work_id: UUID
    recipient_work_id: UUID
    route_ref: str
    kind: Literal["request", "result"]
    payload: JsonValue
    state: Literal["AVAILABLE", "RECEIVED"]
    recipient_grant_version: int
    receiving_generation: str | None = None


class MessageSubmitResult(ClosedModel):
    status: Literal["ok", "conflict", "denied", "stale", "recovery_required"]
    message: PendingMessage | None = None
    reason: Literal[
        "message_identity_conflict", "reply_identity_conflict", "reply_delivery_not_found",
        "reply_sender_not_recipient", "no_current_grant", "grant_version_changed",
        "state_unavailable",
    ] | None = None

    @model_validator(mode="after")
    def exact_shape(self) -> Self:
        if self.status == "ok" and (self.message is None or self.reason is not None):
            raise ValueError("successful submit requires only the exact message")
        if self.status != "ok" and (self.message is not None or self.reason is None):
            raise ValueError("unsuccessful submit requires only its closed reason")
        return self


class MessagePendingRequest(ClosedModel):
    api_version: ApiVersion
    grant_version: int = Field(ge=1)
    cursor: UUID | None = None
    limit: int = Field(default=50, ge=1, le=100)


class MessagePendingResult(ClosedModel):
    status: Literal["ok", "denied", "stale", "recovery_required"] = "ok"
    messages: tuple[PendingMessage, ...] = ()
    next_cursor: UUID | None = None
    has_more: bool = False
    reason: Literal["no_current_grant", "grant_version_changed", "state_unavailable"] | None = None

    @model_validator(mode="after")
    def exact_page(self) -> Self:
        if self.status != "ok":
            if self.messages or self.next_cursor is not None or self.has_more or self.reason is None:
                raise ValueError("failed pending read must not claim message data")
            return self
        if self.reason is not None:
            raise ValueError("successful pending read has no failure reason")
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("cursor presence must match page truncation")
        if self.next_cursor is not None and (
            not self.messages or self.next_cursor != self.messages[-1].delivery_id
        ):
            raise ValueError("next cursor must identify the final returned delivery")
        return self


class RuntimeCurrentness(ClosedModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    generation: str = Field(min_length=1)
    current_generation: str | None = Field(default=None, min_length=1)


class MessageReceiveRequest(ClosedModel):
    api_version: ApiVersion
    delivery_id: UUID
    grant_version: int = Field(ge=1)


class DispositionEvidence(ClosedModel):
    kind: Literal["result", "provider_effect"]
    result_message_id: UUID | None = None
    operation_id: UUID | None = None

    @model_validator(mode="after")
    def exact_reference(self) -> Self:
        if self.kind == "result" and self.result_message_id is not None and self.operation_id is None:
            return self
        if self.kind == "provider_effect" and self.operation_id is not None and self.result_message_id is None:
            return self
        raise ValueError("disposition evidence requires exactly its typed correlation")


class MessageDispositionRequest(ClosedModel):
    api_version: ApiVersion
    delivery_id: UUID
    grant_version: int = Field(ge=1)
    disposition_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: DispositionEvidence

    @model_validator(mode="after")
    def canonical_digest(self) -> Self:
        if self.disposition_digest != _json_digest(self.evidence.model_dump(mode="json")):
            raise ValueError("disposition digest does not match its exact evidence")
        return self


class MessageTransitionResult(ClosedModel):
    status: Literal["ok", "conflict", "denied", "stale", "recovery_required"]
    state: Literal["AVAILABLE", "RECEIVED", "DISPOSITIONED"] | None = None
    disposition_digest: str | None = None
    reason: Literal[
        "no_current_grant", "grant_version_changed", "delivery_not_found",
        "delivery_not_for_current_work", "runtime_currentness_unavailable",
        "runtime_generation_changed", "receiving_binding_changed", "delivery_not_received",
        "delivery_already_dispositioned", "disposition_identity_conflict",
        "result_evidence_missing", "result_evidence_mismatch", "effect_evidence_missing",
        "effect_evidence_unknown", "effect_not_applied", "effect_evidence_mismatch",
        "state_unavailable",
    ] | None = None

    @model_validator(mode="after")
    def exact_shape(self) -> Self:
        if self.status == "ok" and (self.state is None or self.reason is not None):
            raise ValueError("successful transition requires state readback")
        if self.status != "ok" and self.reason is None:
            raise ValueError("unsuccessful transition requires a closed reason")
        return self


def _json_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _digest(route: MessageRoute, request: MessageSubmitRequest) -> str:
    content = {
        "route": route.model_dump(mode="json"),
        "message": request.model_dump(
            mode="json", exclude={"api_version", "message_id", "grant_version"}
        ),
    }
    return _json_digest(content)


def _view(row: Mapping[str, Any]) -> PendingMessage:
    return PendingMessage.model_validate(row)


def _transition(
    status: Literal["ok", "conflict", "denied", "stale", "recovery_required"],
    reason: Any = None, row: Mapping[str, Any] | None = None,
) -> MessageTransitionResult:
    return MessageTransitionResult(
        status=status, reason=reason, state=None if row is None else row["state"],
        disposition_digest=None if row is None else row["disposition_digest"],
    )


class MessageState:
    def __init__(self, engine: AsyncEngine, grants: GrantState):
        self.engine, self.grants = engine, grants

    @staticmethod
    def _admission(
        principal: PrincipalContext, grant: WorkGrant | None, grant_version: int,
    ) -> tuple[Literal["denied", "stale"], Literal[
        "no_current_grant", "grant_version_changed"
    ]] | None:
        if grant is None or grant.principal != principal or not grant.current():
            return "denied", "no_current_grant"
        if grant.version != grant_version:
            return "stale", "grant_version_changed"
        return None

    @staticmethod
    def _runtime(runtime: RuntimeCurrentness) -> tuple[
        Literal["stale", "recovery_required"], Literal[
            "runtime_generation_changed", "runtime_currentness_unavailable"
        ]
    ] | None:
        if runtime.current_generation is None:
            return "recovery_required", "runtime_currentness_unavailable"
        if runtime.generation != runtime.current_generation:
            return "stale", "runtime_generation_changed"
        return None

    async def submit(
        self, principal: PrincipalContext, route: MessageRoute, request: MessageSubmitRequest,
    ) -> MessageSubmitResult:
        try:
            async with self.grants.locked(principal.key) as grant:
                failure = self._admission(principal, grant, request.grant_version)
                if failure is not None:
                    return MessageSubmitResult(status=failure[0], reason=failure[1])
                assert grant is not None
                return await self._store(grant.authority.active_work_id, route, request)
        except (SQLAlchemyError, ValueError):
            return MessageSubmitResult(status="recovery_required", reason="state_unavailable")

    async def _store(
        self, sender_work_id: UUID, route: MessageRoute, request: MessageSubmitRequest,
    ) -> MessageSubmitResult:
        digest = _digest(route, request)
        identity = f"{sender_work_id}:{request.message_id}:{route.recipient_work_id}"
        delivery_id = uuid5(DELIVERY_NAMESPACE, identity)
        projection_id = uuid5(PROJECTION_NAMESPACE, identity)
        async with self.engine.begin() as connection:
            if request.in_reply_to_delivery_id is not None:
                replied = (await connection.execute(select(message_deliveries).where(
                    message_deliveries.c.delivery_id == request.in_reply_to_delivery_id
                ).with_for_update())).mappings().one_or_none()
                if replied is None:
                    return MessageSubmitResult(
                        status="conflict", reason="reply_delivery_not_found"
                    )
                if replied["recipient_work_id"] != sender_work_id:
                    return MessageSubmitResult(
                        status="denied", reason="reply_sender_not_recipient"
                    )
                prior_reply = (await connection.execute(select(
                    messages.c.sender_work_id, messages.c.message_id
                ).where(
                    messages.c.in_reply_to_delivery_id == request.in_reply_to_delivery_id
                ))).one_or_none()
                if prior_reply is not None and prior_reply != (sender_work_id, request.message_id):
                    return MessageSubmitResult(
                        status="conflict", reason="reply_identity_conflict"
                    )
            await connection.execute(insert(messages).values(
                sender_work_id=sender_work_id, message_id=request.message_id,
                route_ref=request.route_ref, kind=request.kind, payload=request.payload,
                digest=digest, in_reply_to_delivery_id=request.in_reply_to_delivery_id,
            ).on_conflict_do_nothing())
            current = (await connection.execute(select(messages.c.digest).where(and_(
                messages.c.sender_work_id == sender_work_id,
                messages.c.message_id == request.message_id,
            )))).scalar_one()
            if current != digest:
                return MessageSubmitResult(status="conflict", reason="message_identity_conflict")
            await connection.execute(insert(message_deliveries).values(
                delivery_id=delivery_id, sender_work_id=sender_work_id,
                message_id=request.message_id, recipient_work_id=route.recipient_work_id,
                recipient_grant_version=route.recipient_grant_version,
            ).on_conflict_do_nothing())
            await connection.execute(insert(message_projection).values(
                projection_id=projection_id, sender_work_id=sender_work_id,
                message_id=request.message_id, operation_id=projection_id,
                provider=route.projection_provider, target=route.projection_target,
            ).on_conflict_do_nothing())
            row = (await connection.execute(self._pending_query().where(
                message_deliveries.c.delivery_id == delivery_id
            ))).mappings().one()
        return MessageSubmitResult(status="ok", message=_view(dict(row)))

    async def pending(
        self, principal: PrincipalContext, request: MessagePendingRequest,
    ) -> MessagePendingResult:
        try:
            async with self.grants.locked(principal.key) as grant:
                failure = self._admission(principal, grant, request.grant_version)
                if failure is not None:
                    return MessagePendingResult(status=failure[0], reason=failure[1])
                assert grant is not None
                query = self._pending_query().where(
                    message_deliveries.c.recipient_work_id == grant.authority.active_work_id,
                    message_deliveries.c.state.in_(("AVAILABLE", "RECEIVED")),
                )
                if request.cursor is not None:
                    query = query.where(message_deliveries.c.delivery_id > request.cursor)
                query = query.order_by(message_deliveries.c.delivery_id).limit(request.limit + 1)
                async with self.engine.connect() as connection:
                    rows = list((await connection.execute(query)).mappings())
                has_more = len(rows) > request.limit
                selected = rows[:request.limit]
                return MessagePendingResult(
                    messages=tuple(_view(dict(row)) for row in selected),
                    next_cursor=selected[-1]["delivery_id"] if has_more else None,
                    has_more=has_more,
                )
        except (SQLAlchemyError, ValueError):
            return MessagePendingResult(status="recovery_required", reason="state_unavailable")

    async def receive(
        self, principal: PrincipalContext, runtime: RuntimeCurrentness,
        request: MessageReceiveRequest,
    ) -> MessageTransitionResult:
        try:
            async with self.grants.locked(principal.key) as grant:
                failure = (self._admission(principal, grant, request.grant_version)
                           or self._runtime(runtime))
                if failure is not None:
                    return _transition(failure[0], failure[1])
                assert grant is not None
                async with self.engine.begin() as connection:
                    raw = (await connection.execute(select(message_deliveries).where(
                        message_deliveries.c.delivery_id == request.delivery_id
                    ).with_for_update())).mappings().one_or_none()
                    row = None if raw is None else dict(raw)
                    failure_result = self._delivery_access(row, grant)
                    if failure_result is not None:
                        return failure_result
                    assert row is not None
                    if row["state"] == "DISPOSITIONED":
                        return _transition("conflict", "delivery_already_dispositioned", row)
                    if row["state"] == "RECEIVED":
                        if (row["recipient_grant_version"] != grant.version
                                or row["receiving_generation"] != runtime.generation):
                            return _transition(
                                "recovery_required", "receiving_binding_changed", row
                            )
                        return _transition("ok", row=row)
                    result = await connection.execute(update(message_deliveries).where(
                        message_deliveries.c.delivery_id == request.delivery_id
                    ).values(
                        state="RECEIVED", recipient_grant_version=grant.version,
                        receiving_generation=runtime.generation, received_at=func.now(),
                    ).returning(*message_deliveries.c))
                    return _transition("ok", row=dict(result.mappings().one()))
        except (SQLAlchemyError, ValueError):
            return _transition("recovery_required", "state_unavailable")

    async def disposition(
        self, principal: PrincipalContext, runtime: RuntimeCurrentness,
        request: MessageDispositionRequest,
    ) -> MessageTransitionResult:
        try:
            async with self.grants.locked(principal.key) as grant:
                failure = (self._admission(principal, grant, request.grant_version)
                           or self._runtime(runtime))
                if failure is not None:
                    return _transition(failure[0], failure[1])
                assert grant is not None
                async with self.engine.begin() as connection:
                    raw = (await connection.execute(select(message_deliveries).where(
                        message_deliveries.c.delivery_id == request.delivery_id
                    ).with_for_update())).mappings().one_or_none()
                    row = None if raw is None else dict(raw)
                    failure_result = self._delivery_access(row, grant)
                    if failure_result is not None:
                        return failure_result
                    assert row is not None
                    if row["state"] == "DISPOSITIONED":
                        if (row["disposition_digest"] == request.disposition_digest
                                and row["evidence"] == request.evidence.model_dump(mode="json")):
                            return _transition("ok", row=row)
                        return _transition("conflict", "disposition_identity_conflict", row)
                    if row["state"] != "RECEIVED":
                        return _transition("conflict", "delivery_not_received", row)
                    if (row["recipient_grant_version"] != grant.version
                            or row["receiving_generation"] != runtime.generation):
                        return _transition("recovery_required", "receiving_binding_changed", row)
                    evidence_failure = await self._evidence_failure(
                        connection, grant, row, request.evidence
                    )
                    if evidence_failure is not None:
                        return _transition("recovery_required", evidence_failure, row)
                    result = await connection.execute(update(message_deliveries).where(
                        message_deliveries.c.delivery_id == request.delivery_id
                    ).values(
                        state="DISPOSITIONED", dispositioned_at=func.now(),
                        disposition_digest=request.disposition_digest,
                        evidence=request.evidence.model_dump(mode="json"),
                    ).returning(*message_deliveries.c))
                    return _transition("ok", row=dict(result.mappings().one()))
        except (SQLAlchemyError, ValueError):
            return _transition("recovery_required", "state_unavailable")

    @staticmethod
    def _delivery_access(
        row: Mapping[str, Any] | None, grant: WorkGrant,
    ) -> MessageTransitionResult | None:
        if row is None:
            return _transition("denied", "delivery_not_found")
        if row["recipient_work_id"] != grant.authority.active_work_id:
            return _transition("denied", "delivery_not_for_current_work")
        return None

    async def _evidence_failure(
        self, connection: Any, grant: WorkGrant, delivery: Mapping[str, Any],
        evidence: DispositionEvidence,
    ) -> Literal[
        "result_evidence_missing", "result_evidence_mismatch", "effect_evidence_missing",
        "effect_evidence_unknown", "effect_not_applied", "effect_evidence_mismatch",
    ] | None:
        if evidence.kind == "result":
            result = (await connection.execute(select(messages).where(and_(
                messages.c.sender_work_id == grant.authority.active_work_id,
                messages.c.message_id == evidence.result_message_id,
            )))).mappings().one_or_none()
            if result is None:
                return "result_evidence_missing"
            if result["in_reply_to_delivery_id"] != delivery["delivery_id"]:
                return "result_evidence_mismatch"
            return None
        assert evidence.operation_id is not None
        previous = await self.grants.previous(
            evidence.operation_id, grant.authority.active_work_id
        )
        if previous is None:
            return "effect_evidence_missing"
        outcome = previous[2]
        if outcome.operation_id != evidence.operation_id:
            return "effect_evidence_mismatch"
        if outcome.effect == "unknown":
            return "effect_evidence_unknown"
        if outcome.effect == "not_sent":
            return "effect_not_applied"
        if outcome.receipt is None or outcome.receipt.work_id != grant.authority.active_work_id:
            return "effect_evidence_mismatch"
        return None

    @staticmethod
    def _pending_query():
        return select(
            message_deliveries.c.delivery_id, message_deliveries.c.message_id,
            message_deliveries.c.sender_work_id, message_deliveries.c.recipient_work_id,
            message_deliveries.c.state, message_deliveries.c.recipient_grant_version,
            message_deliveries.c.receiving_generation, messages.c.route_ref, messages.c.kind,
            messages.c.payload,
        ).join(messages, and_(
            messages.c.sender_work_id == message_deliveries.c.sender_work_id,
            messages.c.message_id == message_deliveries.c.message_id,
        ))
