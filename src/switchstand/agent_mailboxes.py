"""Temporary durable agent-name to MessageState mailbox binding."""

import re
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, Table, Text, UniqueConstraint, select, update
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from .contracts import ClosedModel
from .state import metadata

agent_mailboxes = Table(
    "agent_mailboxes",
    metadata,
    Column("name_key", Text, primary_key=True),
    Column("display_name", Text, nullable=False),
    Column("work_id", PGUUID(as_uuid=True), ForeignKey("work_handles.id", ondelete="RESTRICT"), nullable=False),
    Column("principal_key", Text, nullable=False),
    Column("generation", Integer, nullable=False),
    UniqueConstraint("principal_key"),
    UniqueConstraint("work_id"),
    CheckConstraint("generation >= 1"),
)


class AgentMailbox(ClosedModel):
    name: str = Field(min_length=1, max_length=80)
    name_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9.-]*$")
    work_id: UUID
    principal_key: str = Field(min_length=1)
    generation: int = Field(ge=1)


class AgentMailboxResult(ClosedModel):
    status: Literal["ok", "conflict", "denied", "recovery_required"]
    mailbox: AgentMailbox | None = None
    reason: Literal[
        "name_collision", "principal_already_registered", "work_already_registered", "work_not_bound",
        "mailbox_not_found", "principal_not_registered", "state_unavailable",
        "generation_changed",
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


class AgentMailboxState:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    @staticmethod
    def _view(row) -> AgentMailbox:
        return AgentMailbox(
            name=row["display_name"], name_key=row["name_key"], work_id=row["work_id"],
            principal_key=row["principal_key"], generation=row["generation"],
        )

    async def register(self, name: str, work_id, principal_key: str) -> AgentMailboxResult:
        try:
            key = agent_name_key(name)
            display = " ".join(name.strip().split())
            async with self.engine.begin() as connection:
                bound = (await connection.execute(
                    select(agent_mailboxes).where(agent_mailboxes.c.name_key == key).with_for_update()
                )).mappings().one_or_none()
                if bound is not None:
                    mailbox = self._view(bound)
                    if (
                        mailbox.name == display
                        and mailbox.work_id == work_id
                        and mailbox.principal_key == principal_key
                    ):
                        return AgentMailboxResult(status="ok", mailbox=mailbox)
                    return AgentMailboxResult(status="conflict", reason="name_collision")
                prior = (await connection.execute(
                    select(agent_mailboxes.c.name_key).where(
                        agent_mailboxes.c.principal_key == principal_key
                    ).with_for_update()
                )).scalar_one_or_none()
                if prior is not None:
                    return AgentMailboxResult(
                        status="conflict", reason="principal_already_registered"
                    )
                prior_work = (await connection.execute(
                    select(agent_mailboxes.c.name_key).where(
                        agent_mailboxes.c.work_id == work_id
                    ).with_for_update()
                )).scalar_one_or_none()
                if prior_work is not None:
                    return AgentMailboxResult(
                        status="conflict", reason="work_already_registered"
                    )
                handle = (await connection.execute(
                    select(agent_mailboxes.metadata.tables["work_handles"].c.id).where(
                        agent_mailboxes.metadata.tables["work_handles"].c.id == work_id
                    ).with_for_update()
                )).scalar_one_or_none()
                if handle is None:
                    return AgentMailboxResult(status="denied", reason="work_not_bound")
                await connection.execute(insert(agent_mailboxes).values(
                    name_key=key, display_name=display, work_id=work_id,
                    principal_key=principal_key, generation=1,
                ))
                row = (await connection.execute(
                    select(agent_mailboxes).where(agent_mailboxes.c.name_key == key)
                )).mappings().one()
                return AgentMailboxResult(status="ok", mailbox=self._view(row))
        except (IntegrityError, SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    async def by_name(self, name: str) -> AgentMailboxResult:
        try:
            key = agent_name_key(name)
            async with self.engine.connect() as connection:
                row = (await connection.execute(
                    select(agent_mailboxes).where(agent_mailboxes.c.name_key == key)
                )).mappings().one_or_none()
            if row is None:
                return AgentMailboxResult(status="denied", reason="mailbox_not_found")
            return AgentMailboxResult(status="ok", mailbox=self._view(row))
        except (SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    async def by_work_id(self, work_id: UUID) -> AgentMailboxResult:
        try:
            async with self.engine.connect() as connection:
                row = (await connection.execute(
                    select(agent_mailboxes).where(agent_mailboxes.c.work_id == work_id)
                )).mappings().one_or_none()
            if row is None:
                return AgentMailboxResult(status="denied", reason="mailbox_not_found")
            return AgentMailboxResult(status="ok", mailbox=self._view(row))
        except (SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    async def for_principal(self, principal_key: str) -> AgentMailboxResult:
        try:
            async with self.engine.connect() as connection:
                row = (await connection.execute(
                    select(agent_mailboxes).where(
                        agent_mailboxes.c.principal_key == principal_key
                    )
                )).mappings().one_or_none()
            if row is None:
                return AgentMailboxResult(status="denied", reason="principal_not_registered")
            return AgentMailboxResult(status="ok", mailbox=self._view(row))
        except (SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    async def takeover(
        self, name: str, expected_generation: int, principal_key: str,
    ) -> AgentMailboxResult:
        """Trusted control path only: preserve mailbox WorkId and fence the old actor."""
        try:
            key = agent_name_key(name)
            async with self.engine.begin() as connection:
                row = (await connection.execute(
                    select(agent_mailboxes).where(agent_mailboxes.c.name_key == key).with_for_update()
                )).mappings().one_or_none()
                if row is None:
                    return AgentMailboxResult(status="denied", reason="mailbox_not_found")
                if row["generation"] != expected_generation:
                    return AgentMailboxResult(status="conflict", reason="generation_changed")
                prior = (await connection.execute(
                    select(agent_mailboxes.c.name_key).where(
                        agent_mailboxes.c.principal_key == principal_key,
                        agent_mailboxes.c.name_key != key,
                    )
                )).scalar_one_or_none()
                if prior is not None:
                    return AgentMailboxResult(
                        status="conflict", reason="principal_already_registered"
                    )
                result = await connection.execute(
                    update(agent_mailboxes).where(agent_mailboxes.c.name_key == key).values(
                        principal_key=principal_key, generation=expected_generation + 1
                    ).returning(*agent_mailboxes.c)
                )
                return AgentMailboxResult(status="ok", mailbox=self._view(result.mappings().one()))
        except (IntegrityError, SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")
