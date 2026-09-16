"""Durable message envelopes and per-recipient delivery state."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal, Self
from uuid import UUID, uuid5

from pydantic import Field, JsonValue, model_validator
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

    @staticmethod
    def _pending_query():
        return select(
            message_deliveries.c.delivery_id, message_deliveries.c.message_id,
            message_deliveries.c.sender_work_id, message_deliveries.c.recipient_work_id,
            message_deliveries.c.state, message_deliveries.c.recipient_grant_version,
            messages.c.route_ref, messages.c.kind,
            messages.c.payload,
        ).join(messages, and_(
            messages.c.sender_work_id == message_deliveries.c.sender_work_id,
            messages.c.message_id == message_deliveries.c.message_id,
        ))
