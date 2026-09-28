"""Durable named endpoints for ordinary agent messaging."""

import hashlib
import re
from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import (
    CheckConstraint,
    Column,
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
                bound = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.name_key == key
                ).with_for_update())).mappings().one_or_none()
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
                ).with_for_update())).scalar_one_or_none()
                if prior is not None:
                    return AgentMailboxResult(status="conflict", reason="session_already_registered")
                await connection.execute(insert(agent_mailboxes).values(
                    name_key=key, display_name=display, endpoint_id=uuid4(),
                    principal_key=principal_key, session_key=session_key, generation=1,
                ))
                row = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.name_key == key
                ))).mappings().one()
                return AgentMailboxResult(status="ok", mailbox=self._view(row))
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
