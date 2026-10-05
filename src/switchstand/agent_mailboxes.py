"""Durable named endpoints for ordinary agent messaging."""

import hashlib
import re
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import Field, model_validator
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from .contracts import ClosedModel
from .state import metadata

agent_mailboxes = Table(
    "agent_mailboxes", metadata,
    Column("name_key", Text, primary_key=True),
    Column("display_name", Text, nullable=False),
    Column("endpoint_id", PGUUID(as_uuid=True), nullable=False),
    Column("principal_key", Text, nullable=False),
    Column("session_key", Text, nullable=False),
    Column("generation", Integer, nullable=False),
    UniqueConstraint("endpoint_id"),
    UniqueConstraint("principal_key", "session_key"),
    CheckConstraint("generation >= 1"),
)

agent_mailbox_transfer_requests = Table(
    "agent_mailbox_transfer_requests", metadata,
    Column("request_id", PGUUID(as_uuid=True), primary_key=True),
    Column("name_key", Text, nullable=False),
    Column("endpoint_id", PGUUID(as_uuid=True), nullable=False),
    Column("expected_generation", Integer, nullable=False),
    Column("source_principal_key", Text, nullable=False),
    Column("source_session_key", Text, nullable=False),
    Column("destination_principal_key", Text, nullable=False),
    Column("destination_session_key", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("reason", Text),
    Column("resulting_generation", Integer),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True)),
    CheckConstraint("expected_generation >= 1"),
    CheckConstraint("resulting_generation IS NULL OR resulting_generation >= 2"),
    CheckConstraint("status IN ('PENDING', 'APPROVED', 'STALE', 'CONFLICT')"),
)


class AgentMailbox(ClosedModel):
    name: str = Field(min_length=1, max_length=80)
    name_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9.-]*$")
    endpoint_id: UUID
    principal_key: str = Field(min_length=1)
    session_key: str = Field(min_length=1)
    generation: int = Field(ge=1)


class AgentMailboxResult(ClosedModel):
    status: Literal["ok", "conflict", "denied", "recovery_required"]
    mailbox: AgentMailbox | None = None
    reason: Literal[
        "name_collision", "session_already_registered", "mailbox_not_found",
        "agent_not_registered", "principal_mismatch", "state_unavailable",
    ] | None = None

    @model_validator(mode="after")
    def exact_shape(self):
        if (self.status == "ok") != (self.mailbox is not None):
            raise ValueError("mailbox result shape invalid")
        if (self.status == "ok") == (self.reason is not None):
            raise ValueError("mailbox reason shape invalid")
        return self


class AgentMailboxTransferResult(ClosedModel):
    status: Literal["pending", "approved", "stale", "conflict", "denied", "recovery_required"]
    request_id: UUID | None = None
    name: str | None = None
    endpoint_id: UUID | None = None
    generation: int | None = Field(default=None, ge=1)
    reason: Literal[
        "mailbox_not_found", "same_principal_use_takeover", "session_already_registered",
        "mailbox_changed", "state_unavailable",
    ] | None = None


def agent_name_key(value: str) -> str:
    visible = " ".join(value.strip().split())
    key = re.sub(r"[^a-z0-9]+", "-", visible.casefold()).strip("-")
    if not visible or not key or not key[0].isalpha() or len(visible) > 80 or len(key) > 80:
        raise ValueError("invalid agent name")
    return key


def chat_session_key(value: str) -> str:
    if not value or len(value) > 4096:
        raise ValueError("invalid chat session")
    return hashlib.sha256(value.encode()).hexdigest()


class AgentMailboxState:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    @staticmethod
    def _view(row: RowMapping) -> AgentMailbox:
        return AgentMailbox(
            name=cast(str, row["display_name"]), name_key=cast(str, row["name_key"]),
            endpoint_id=cast(UUID, row["endpoint_id"]),
            principal_key=cast(str, row["principal_key"]),
            session_key=cast(str, row["session_key"]), generation=cast(int, row["generation"]),
        )

    async def register_agent(self, name: str, principal_key: str, chat_session: str) -> AgentMailboxResult:
        try:
            key, display = agent_name_key(name), " ".join(name.strip().split())
            session_key = chat_session_key(chat_session)
            async with self.engine.begin() as connection:
                await connection.execute(insert(agent_mailboxes).values(
                    name_key=key, display_name=display, endpoint_id=uuid4(),
                    principal_key=principal_key, session_key=session_key, generation=1,
                ).on_conflict_do_nothing())
                bound = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.name_key == key
                ))).mappings().one_or_none()
                if bound is not None:
                    mailbox = self._view(bound)
                    if (mailbox.name, mailbox.principal_key, mailbox.session_key) == (
                        display, principal_key, session_key,
                    ):
                        return AgentMailboxResult(status="ok", mailbox=mailbox)
                    return AgentMailboxResult(status="conflict", reason="name_collision")
                prior = (await connection.execute(select(agent_mailboxes.c.name_key).where(
                    agent_mailboxes.c.principal_key == principal_key,
                    agent_mailboxes.c.session_key == session_key,
                ))).scalar_one_or_none()
                if prior is not None:
                    return AgentMailboxResult(status="conflict", reason="session_already_registered")
                return AgentMailboxResult(status="recovery_required", reason="state_unavailable")
        except (IntegrityError, SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    async def by_name(self, name: str) -> AgentMailboxResult:
        try:
            key = agent_name_key(name)
            async with self.engine.connect() as connection:
                row = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.name_key == key
                ))).mappings().one_or_none()
            if row is None:
                return AgentMailboxResult(status="denied", reason="mailbox_not_found")
            return AgentMailboxResult(status="ok", mailbox=self._view(row))
        except (SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    async def by_endpoint_id(self, endpoint_id: UUID) -> AgentMailboxResult:
        try:
            async with self.engine.connect() as connection:
                row = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.endpoint_id == endpoint_id
                ))).mappings().one_or_none()
            if row is None:
                return AgentMailboxResult(status="denied", reason="mailbox_not_found")
            return AgentMailboxResult(status="ok", mailbox=self._view(row))
        except (SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    async def for_actor(self, principal_key: str, chat_session: str) -> AgentMailboxResult:
        try:
            session_key = chat_session_key(chat_session)
            async with self.engine.connect() as connection:
                row = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.principal_key == principal_key,
                    agent_mailboxes.c.session_key == session_key,
                ))).mappings().one_or_none()
            if row is None:
                return AgentMailboxResult(status="denied", reason="agent_not_registered")
            return AgentMailboxResult(status="ok", mailbox=self._view(row))
        except (SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    async def takeover(self, name: str, principal_key: str, replacement_session: str) -> AgentMailboxResult:
        try:
            key, session_key = agent_name_key(name), chat_session_key(replacement_session)
            async with self.engine.begin() as connection:
                row = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.name_key == key
                ).with_for_update())).mappings().one_or_none()
                if row is None:
                    return AgentMailboxResult(status="denied", reason="mailbox_not_found")
                if row["principal_key"] != principal_key:
                    return AgentMailboxResult(status="denied", reason="principal_mismatch")
                if row["session_key"] == session_key:
                    return AgentMailboxResult(status="ok", mailbox=self._view(row))
                prior = (await connection.execute(select(agent_mailboxes.c.name_key).where(
                    agent_mailboxes.c.principal_key == principal_key,
                    agent_mailboxes.c.session_key == session_key,
                    agent_mailboxes.c.name_key != key,
                ).with_for_update())).scalar_one_or_none()
                if prior is not None:
                    return AgentMailboxResult(status="conflict", reason="session_already_registered")
                moved = (await connection.execute(update(agent_mailboxes).where(
                    agent_mailboxes.c.name_key == key,
                    agent_mailboxes.c.generation == row["generation"],
                ).values(
                    session_key=session_key, generation=row["generation"] + 1,
                ).returning(*agent_mailboxes.c))).mappings().one_or_none()
                if moved is None:
                    return AgentMailboxResult(status="recovery_required", reason="state_unavailable")
                return AgentMailboxResult(status="ok", mailbox=self._view(moved))
        except (IntegrityError, SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    async def request_transfer(
        self, name: str, destination_principal_key: str, destination_session: str,
    ) -> AgentMailboxTransferResult:
        """Record an exact cross-principal transfer request without moving the mailbox."""
        try:
            key = agent_name_key(name)
            destination_session_key = chat_session_key(destination_session)
            async with self.engine.begin() as connection:
                row = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.name_key == key
                ).with_for_update())).mappings().one_or_none()
                if row is None:
                    return AgentMailboxTransferResult(status="denied", reason="mailbox_not_found")
                if row["principal_key"] == destination_principal_key:
                    return AgentMailboxTransferResult(status="denied", reason="same_principal_use_takeover")
                prior = (await connection.execute(select(agent_mailboxes.c.name_key).where(
                    agent_mailboxes.c.principal_key == destination_principal_key,
                    agent_mailboxes.c.session_key == destination_session_key,
                ).with_for_update())).scalar_one_or_none()
                if prior is not None:
                    return AgentMailboxTransferResult(status="conflict", reason="session_already_registered")
                identity = "\x00".join((
                    str(row["endpoint_id"]), str(row["generation"]),
                    cast(str, row["principal_key"]), cast(str, row["session_key"]),
                    destination_principal_key, destination_session_key,
                ))
                request_id = uuid5(NAMESPACE_URL, f"switchstand-agent-transfer:{identity}")
                await connection.execute(insert(agent_mailbox_transfer_requests).values(
                    request_id=request_id,
                    name_key=key,
                    endpoint_id=row["endpoint_id"],
                    expected_generation=row["generation"],
                    source_principal_key=row["principal_key"],
                    source_session_key=row["session_key"],
                    destination_principal_key=destination_principal_key,
                    destination_session_key=destination_session_key,
                    status="PENDING",
                    created_at=datetime.now(UTC),
                ).on_conflict_do_nothing(index_elements=["request_id"]))
                request = (await connection.execute(select(
                    agent_mailbox_transfer_requests
                ).where(
                    agent_mailbox_transfer_requests.c.request_id == request_id
                ))).mappings().one()
                if request["status"] == "APPROVED":
                    return AgentMailboxTransferResult(
                        status="approved", request_id=request_id,
                        name=cast(str, row["display_name"]), endpoint_id=row["endpoint_id"],
                        generation=cast(int, request["resulting_generation"]),
                    )
                if request["status"] != "PENDING":
                    return AgentMailboxTransferResult(
                        status=cast(Literal["stale", "conflict"], request["status"].lower()),
                        request_id=request_id,
                        reason=cast(Literal["mailbox_changed", "session_already_registered"],
                                    request["reason"]),
                    )
                return AgentMailboxTransferResult(
                    status="pending", request_id=request_id,
                    name=cast(str, row["display_name"]), endpoint_id=row["endpoint_id"], generation=cast(int, row["generation"]),
                )
        except (IntegrityError, SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxTransferResult(status="recovery_required", reason="state_unavailable")

    async def approve_transfer(self, request_id: UUID) -> AgentMailboxTransferResult:
        """Atomically approve one exact host-selected request and fence the old owner."""
        try:
            async with self.engine.begin() as connection:
                request = (await connection.execute(select(
                    agent_mailbox_transfer_requests
                ).where(
                    agent_mailbox_transfer_requests.c.request_id == request_id
                ).with_for_update())).mappings().one_or_none()
                if request is None:
                    return AgentMailboxTransferResult(status="denied", request_id=request_id,
                                                      reason="mailbox_not_found")
                row = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.name_key == request["name_key"]
                ).with_for_update())).mappings().one_or_none()
                if request["status"] == "APPROVED":
                    return AgentMailboxTransferResult(
                        status="approved", request_id=request_id,
                        name=cast(str, row["display_name"]) if row is not None else request["name_key"],
                        endpoint_id=cast(UUID, request["endpoint_id"]),
                        generation=cast(int, request["resulting_generation"]),
                    )
                if request["status"] != "PENDING":
                    return AgentMailboxTransferResult(
                        status=cast(Literal["stale", "conflict"], request["status"].lower()),
                        request_id=request_id,
                        reason=cast(Literal["mailbox_changed", "session_already_registered"],
                                    request["reason"]),
                    )
                preimage = (
                    request["endpoint_id"], request["expected_generation"],
                    request["source_principal_key"], request["source_session_key"],
                )
                current = None if row is None else (
                    row["endpoint_id"], row["generation"], row["principal_key"], row["session_key"],
                )
                if current != preimage:
                    await connection.execute(update(agent_mailbox_transfer_requests).where(
                        agent_mailbox_transfer_requests.c.request_id == request_id
                    ).values(status="STALE", reason="mailbox_changed", resolved_at=datetime.now(UTC)))
                    return AgentMailboxTransferResult(status="stale", request_id=request_id,
                                                      reason="mailbox_changed")
                collision = (await connection.execute(select(agent_mailboxes.c.name_key).where(
                    agent_mailboxes.c.principal_key == request["destination_principal_key"],
                    agent_mailboxes.c.session_key == request["destination_session_key"],
                    agent_mailboxes.c.name_key != request["name_key"],
                ).with_for_update())).scalar_one_or_none()
                if collision is not None:
                    await connection.execute(update(agent_mailbox_transfer_requests).where(
                        agent_mailbox_transfer_requests.c.request_id == request_id
                    ).values(
                        status="CONFLICT", reason="session_already_registered",
                        resolved_at=datetime.now(UTC),
                    ))
                    return AgentMailboxTransferResult(
                        status="conflict", request_id=request_id,
                        reason="session_already_registered",
                    )
                assert row is not None
                generation = cast(int, row["generation"]) + 1
                moved = (await connection.execute(update(agent_mailboxes).where(
                    agent_mailboxes.c.name_key == request["name_key"],
                    agent_mailboxes.c.generation == request["expected_generation"],
                ).values(
                    principal_key=request["destination_principal_key"],
                    session_key=request["destination_session_key"],
                    generation=generation,
                ).returning(*agent_mailboxes.c))).mappings().one_or_none()
                if moved is None:
                    raise RuntimeError("locked mailbox changed")
                await connection.execute(update(agent_mailbox_transfer_requests).where(
                    agent_mailbox_transfer_requests.c.request_id == request_id
                ).values(
                    status="APPROVED", reason=None, resulting_generation=generation,
                    resolved_at=datetime.now(UTC),
                ))
                return AgentMailboxTransferResult(
                    status="approved", request_id=request_id,
                    name=cast(str, moved["display_name"]), endpoint_id=moved["endpoint_id"],
                    generation=generation,
                )
        except (IntegrityError, SQLAlchemyError, RuntimeError, TypeError, ValueError):
            return AgentMailboxTransferResult(status="recovery_required", request_id=request_id,
                                              reason="state_unavailable")
