"""Durable message envelopes and per-recipient delivery state."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid5

from pydantic import Field, JsonValue
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
from sqlalchemy.ext.asyncio import AsyncEngine

from .contracts import ApiVersion, ClosedModel
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
    projection_provider: str = Field(min_length=1)
    projection_target: str = Field(min_length=1)


class MessageSubmitRequest(ClosedModel):
    api_version: ApiVersion
    message_id: UUID
    route_ref: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    payload: JsonValue
    in_reply_to_delivery_id: UUID | None = None


class PendingMessage(ClosedModel):
    delivery_id: UUID
    message_id: UUID
    sender_work_id: UUID
    recipient_work_id: UUID
    route_ref: str
    kind: str
    payload: JsonValue
    state: str


class MessageSubmitResult(ClosedModel):
    status: str
    message: PendingMessage | None = None
    reason: str | None = None


class MessagePendingResult(ClosedModel):
    status: str
    messages: tuple[PendingMessage, ...] = ()
    next_cursor: UUID | None = None
    has_more: bool = False
    reason: str | None = None


def _digest(request: MessageSubmitRequest) -> str:
    content = request.model_dump(mode="json", exclude={"api_version", "message_id"})
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _view(row: Mapping[str, Any]) -> PendingMessage:
    return PendingMessage.model_validate(row)


class MessageState:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def submit(
        self, sender_work_id: UUID, grant_version: int,
        route: MessageRoute, request: MessageSubmitRequest,
    ) -> MessageSubmitResult:
        digest = _digest(request)
        identity = f"{sender_work_id}:{request.message_id}:{route.recipient_work_id}"
        delivery_id = uuid5(DELIVERY_NAMESPACE, identity)
        projection_id = uuid5(PROJECTION_NAMESPACE, identity)
        async with self.engine.begin() as connection:
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
                recipient_grant_version=grant_version,
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
        self, recipient_work_id: UUID, cursor: UUID | None, limit: int,
    ) -> MessagePendingResult:
        query = self._pending_query().where(
            message_deliveries.c.recipient_work_id == recipient_work_id,
            message_deliveries.c.state.in_(("AVAILABLE", "RECEIVED")),
        )
        if cursor is not None:
            query = query.where(message_deliveries.c.delivery_id > cursor)
        query = query.order_by(message_deliveries.c.delivery_id).limit(limit + 1)
        async with self.engine.connect() as connection:
            rows = list((await connection.execute(query)).mappings())
        has_more = len(rows) > limit
        selected = rows[:limit]
        return MessagePendingResult(
            status="ok", messages=tuple(_view(dict(row)) for row in selected),
            next_cursor=selected[-1]["delivery_id"] if has_more else None,
            has_more=has_more,
        )

    @staticmethod
    def _pending_query():
        return select(
            message_deliveries.c.delivery_id, message_deliveries.c.message_id,
            message_deliveries.c.sender_work_id, message_deliveries.c.recipient_work_id,
            message_deliveries.c.state, messages.c.route_ref, messages.c.kind, messages.c.payload,
        ).join(messages, and_(
            messages.c.sender_work_id == message_deliveries.c.sender_work_id,
            messages.c.message_id == message_deliveries.c.message_id,
        ))
