"""Durable named endpoints for ordinary agent messaging."""

import hashlib
import os
import re
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import bcrypt
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
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..contracts import ClosedModel
from ..state import metadata

CODEX_COORDINATOR_NAME = "Codex Coordinator"
CODEX_COORDINATOR_KEY = "codex-coordinator"
LEGACY_ROOT_KEY = "root"
PROTECTED_TAKEOVER_HASHES = {
    "interface": "SWITCHSTAND_INTERFACE_TAKEOVER_PASSWORD_HASH",
    "coordinator": "SWITCHSTAND_COORDINATOR_TAKEOVER_PASSWORD_HASH",
}

agent_mailboxes = Table(
    "agent_mailboxes", metadata,
    Column("name_key", Text, primary_key=True),
    Column("display_name", Text, nullable=False),
    Column("endpoint_id", PGUUID(as_uuid=True), nullable=False),
    Column("principal_key", Text, nullable=False),
    Column("session_key", Text, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("agent_type", Text),
    UniqueConstraint("endpoint_id"),
    UniqueConstraint("principal_key", "session_key"),
    CheckConstraint("generation >= 1"),
    CheckConstraint("agent_type IS NULL OR agent_type IN ('codex', 'claude', 'chatgpt')",
                    name="ck_mailbox_agent_type"),
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
    Column("destination_agent_type", Text),
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
    agent_type: Literal["codex", "claude", "chatgpt"] | None = None


class AgentMailboxResult(ClosedModel):
    status: Literal["ok", "conflict", "denied", "recovery_required"]
    mailbox: AgentMailbox | None = None
    reason: Literal[
        "name_collision", "session_already_registered", "mailbox_not_found",
        "agent_not_registered", "principal_mismatch", "reserved_name_requires_takeover",
        "takeover_password_required",
        "state_unavailable",
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
        "takeover_password_required",
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


def session_agent_type(session: str) -> Literal["codex", "claude", "chatgpt"]:
    """Classify the trusted runtime identity before its session is hashed."""
    if session.startswith("codex:"):
        return "codex"
    if session.startswith("claude:"):
        return "claude"
    return "chatgpt"


def coordinator_name_keys(value: str) -> tuple[str, ...]:
    """Resolve the canonical Coordinator name and its temporary /root alias."""
    key = agent_name_key(value)
    if key in {CODEX_COORDINATOR_KEY, LEGACY_ROOT_KEY}:
        return CODEX_COORDINATOR_KEY, LEGACY_ROOT_KEY
    return (key,)


class AgentMailboxState:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    @staticmethod
    def _takeover_password_allowed(key: str, password: str | None) -> bool:
        setting = PROTECTED_TAKEOVER_HASHES.get(key)
        if setting is None:
            return True
        try:
            return (
                password is not None and len(password.encode()) <= 72
                and bcrypt.checkpw(password.encode(), os.environ.get(setting, "").encode())
            )
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _view(row: RowMapping) -> AgentMailbox:
        return AgentMailbox(
            name=cast(str, row["display_name"]), name_key=cast(str, row["name_key"]),
            endpoint_id=cast(UUID, row["endpoint_id"]),
            principal_key=cast(str, row["principal_key"]),
            session_key=cast(str, row["session_key"]), generation=cast(int, row["generation"]),
            agent_type=row["agent_type"],
        )

    async def register_agent(self, name: str, principal_key: str, chat_session: str) -> AgentMailboxResult:
        try:
            key, display = agent_name_key(name), " ".join(name.strip().split())
            session_key = chat_session_key(chat_session)
            async with self.engine.begin() as connection:
                prior = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.principal_key == principal_key,
                    agent_mailboxes.c.session_key == session_key,
                ).with_for_update())).mappings().one_or_none()
                if prior is not None:
                    prior = (await connection.execute(update(agent_mailboxes).where(
                        agent_mailboxes.c.name_key == prior["name_key"],
                    ).values(agent_type=session_agent_type(chat_session)).returning(
                        *agent_mailboxes.c))).mappings().one()
                    # Registration is idempotent for the already-bound session, including
                    # an existing /root. It never renames or takes over that mailbox.
                    return AgentMailboxResult(status="ok", mailbox=self._view(prior))
                if key in {CODEX_COORDINATOR_KEY, LEGACY_ROOT_KEY}:
                    return AgentMailboxResult(
                        status="denied", reason="reserved_name_requires_takeover",
                    )
                await connection.execute(insert(agent_mailboxes).values(
                    name_key=key, display_name=display, endpoint_id=uuid4(),
                    principal_key=principal_key, session_key=session_key, generation=1,
                    agent_type=session_agent_type(chat_session),
                ).on_conflict_do_nothing())
                prior = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.principal_key == principal_key,
                    agent_mailboxes.c.session_key == session_key,
                ))).mappings().one_or_none()
                if prior is not None:
                    return AgentMailboxResult(status="ok", mailbox=self._view(prior))
                bound = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.name_key == key
                ))).mappings().one_or_none()
                if bound is not None:
                    return AgentMailboxResult(status="conflict", reason="name_collision")
                return AgentMailboxResult(status="recovery_required", reason="state_unavailable")
        except (IntegrityError, SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    async def by_name(
        self, name: str, connection: AsyncConnection | None = None,
    ) -> AgentMailboxResult:
        try:
            keys = coordinator_name_keys(name)
            if connection is None:
                async with self.engine.connect() as owned:
                    rows = (await owned.execute(select(agent_mailboxes).where(
                        agent_mailboxes.c.name_key.in_(keys)
                    ))).mappings().all()
            else:
                rows = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.name_key.in_(keys)
                ))).mappings().all()
            if len(rows) > 1:
                return AgentMailboxResult(status="recovery_required", reason="state_unavailable")
            row = rows[0] if rows else None
            if row is None:
                return AgentMailboxResult(status="denied", reason="mailbox_not_found")
            return AgentMailboxResult(status="ok", mailbox=self._view(row))
        except (SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    async def by_endpoint_id(
        self, endpoint_id: UUID, connection: AsyncConnection | None = None,
    ) -> AgentMailboxResult:
        try:
            if connection is None:
                async with self.engine.connect() as owned:
                    row = (await owned.execute(select(agent_mailboxes).where(
                        agent_mailboxes.c.endpoint_id == endpoint_id
                    ))).mappings().one_or_none()
            else:
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

    async def for_session(self, chat_session: str) -> AgentMailboxResult:
        """Resolve a local runtime session without accepting a caller-selected name."""
        try:
            session_key = chat_session_key(chat_session)
            async with self.engine.connect() as connection:
                rows = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.session_key == session_key,
                ).limit(2))).mappings().all()
            if not rows:
                return AgentMailboxResult(status="denied", reason="agent_not_registered")
            if len(rows) != 1:
                return AgentMailboxResult(status="recovery_required", reason="state_unavailable")
            return AgentMailboxResult(status="ok", mailbox=self._view(rows[0]))
        except (SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    async def takeover(
        self, name: str, principal_key: str, replacement_session: str, *,
        allow_principal_change: bool = False, password: str | None = None,
    ) -> AgentMailboxResult:
        try:
            key, session_key = agent_name_key(name), chat_session_key(replacement_session)
            if not self._takeover_password_allowed(key, password):
                return AgentMailboxResult(status="denied", reason="takeover_password_required")
            async with self.engine.begin() as connection:
                row = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.name_key == key
                ).with_for_update())).mappings().one_or_none()
                if row is None:
                    prior = (await connection.execute(select(agent_mailboxes).where(
                        agent_mailboxes.c.principal_key == principal_key,
                        agent_mailboxes.c.session_key == session_key,
                    ).with_for_update())).mappings().one_or_none()
                    if prior is not None and prior["name_key"] in {
                        CODEX_COORDINATOR_KEY, LEGACY_ROOT_KEY, *PROTECTED_TAKEOVER_HASHES,
                    }:
                        return AgentMailboxResult(status="conflict", reason="session_already_registered")
                    if prior is None:
                        await connection.execute(insert(agent_mailboxes).values(
                            name_key=key, display_name=" ".join(name.strip().split()),
                            endpoint_id=uuid4(), principal_key=principal_key,
                            session_key=session_key, generation=1,
                            agent_type=session_agent_type(replacement_session),
                        ).on_conflict_do_nothing())
                    else:
                        await connection.execute(update(agent_mailboxes).where(
                            agent_mailboxes.c.name_key == prior["name_key"],
                        ).values(name_key=key, display_name=" ".join(name.strip().split())))
                    row = (await connection.execute(select(agent_mailboxes).where(
                        agent_mailboxes.c.name_key == key
                    ).with_for_update())).mappings().one_or_none()
                    if row is None:
                        return AgentMailboxResult(status="recovery_required", reason="state_unavailable")
                if row["principal_key"] != principal_key and not allow_principal_change:
                    return AgentMailboxResult(status="denied", reason="principal_mismatch")
                if row["principal_key"] == principal_key and row["session_key"] == session_key:
                    row = (await connection.execute(update(agent_mailboxes).where(
                        agent_mailboxes.c.name_key == key,
                    ).values(agent_type=session_agent_type(replacement_session)).returning(
                        *agent_mailboxes.c))).mappings().one()
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
                    principal_key=principal_key, session_key=session_key,
                    agent_type=session_agent_type(replacement_session),
                    generation=row["generation"] + 1,
                ).returning(*agent_mailboxes.c))).mappings().one_or_none()
                if moved is None:
                    return AgentMailboxResult(status="recovery_required", reason="state_unavailable")
                return AgentMailboxResult(status="ok", mailbox=self._view(moved))
        except (IntegrityError, SQLAlchemyError, TypeError, ValueError):
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    async def takeover_coordinator(
        self, principal_key: str, replacement_session: str, *,
        allow_principal_change: bool = False,
    ) -> AgentMailboxResult:
        """Move the Coordinator to an authorized caller and canonicalize its name."""
        try:
            session_key = chat_session_key(replacement_session)
            async with self.engine.begin() as connection:
                rows = (await connection.execute(select(agent_mailboxes).where(
                    agent_mailboxes.c.name_key.in_((
                        CODEX_COORDINATOR_KEY, LEGACY_ROOT_KEY,
                    ))
                ).with_for_update())).mappings().all()
                if not rows:
                    return AgentMailboxResult(status="denied", reason="mailbox_not_found")
                if len(rows) > 1:
                    return AgentMailboxResult(
                        status="recovery_required", reason="state_unavailable"
                    )
                row = rows[0]
                if row["principal_key"] != principal_key and not allow_principal_change:
                    return AgentMailboxResult(status="denied", reason="principal_mismatch")
                if (
                    row["principal_key"] == principal_key
                    and row["session_key"] == session_key
                    and row["name_key"] == CODEX_COORDINATOR_KEY
                    and row["agent_type"] == session_agent_type(replacement_session)
                ):
                    return AgentMailboxResult(status="ok", mailbox=self._view(row))
                prior = (await connection.execute(select(agent_mailboxes.c.name_key).where(
                    agent_mailboxes.c.principal_key == principal_key,
                    agent_mailboxes.c.session_key == session_key,
                    agent_mailboxes.c.name_key != row["name_key"],
                ).with_for_update())).scalar_one_or_none()
                if prior is not None:
                    return AgentMailboxResult(
                        status="conflict", reason="session_already_registered"
                    )
                moved = (await connection.execute(update(agent_mailboxes).where(
                    agent_mailboxes.c.name_key == row["name_key"],
                    agent_mailboxes.c.generation == row["generation"],
                ).values(
                    name_key=CODEX_COORDINATOR_KEY,
                    display_name=CODEX_COORDINATOR_NAME,
                    principal_key=principal_key,
                    session_key=session_key,
                    agent_type=session_agent_type(replacement_session),
                    generation=row["generation"] + 1,
                ).returning(*agent_mailboxes.c))).mappings().one_or_none()
                if moved is None:
                    return AgentMailboxResult(
                        status="recovery_required", reason="state_unavailable"
                    )
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
                    destination_agent_type=session_agent_type(destination_session),
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

    async def approve_transfer(
        self, request_id: UUID, *, password: str | None = None,
    ) -> AgentMailboxTransferResult:
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
                if not self._takeover_password_allowed(request["name_key"], password):
                    return AgentMailboxTransferResult(
                        status="denied", request_id=request_id, reason="takeover_password_required",
                    )
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
                    agent_type=request["destination_agent_type"],
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
