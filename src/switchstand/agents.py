"""Durable ordinary-agent names and replacement fencing."""

from collections.abc import Mapping
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import Column, DateTime, ForeignKey, Integer, Table, Text, UniqueConstraint, func, select, text, update
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from .contracts import ClosedModel
from .state import metadata


agent_bindings = Table(
    "agent_bindings",
    metadata,
    Column("agent_id", PGUUID(as_uuid=True), primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("mailbox_work_id", PGUUID(as_uuid=True), ForeignKey("work_handles.id", ondelete="RESTRICT"), nullable=False, unique=True),
    Column("principal_key", Text, nullable=False),
    Column("session_generation", Text, nullable=False),
    Column("binding_generation", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("principal_key", "session_generation"),
)


class AgentIdentity(ClosedModel):
    agent_id: UUID
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    binding_generation: int = Field(ge=1)


class AgentDirectoryResult(ClosedModel):
    status: str
    identity: AgentIdentity | None = None
    reason: str | None = None

    def model_post_init(self, __context: object) -> None:
        if self.status == "ok":
            if self.identity is None or self.reason is not None:
                raise ValueError("successful agent result requires only identity")
        elif self.identity is not None or self.reason is None:
            raise ValueError("failed agent result requires only reason")


class AgentBinding:
    def __init__(
        self, agent_id: UUID, name: str, mailbox_work_id: UUID, principal_key: str,
        session_generation: str, binding_generation: int,
    ):
        self.agent_id = agent_id
        self.name = name
        self.mailbox_work_id = mailbox_work_id
        self.principal_key = principal_key
        self.session_generation = session_generation
        self.binding_generation = binding_generation

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> "AgentBinding":
        return cls(
            UUID(str(row["agent_id"])), str(row["name"]), UUID(str(row["mailbox_work_id"])),
            str(row["principal_key"]), str(row["session_generation"]),
            int(row["binding_generation"]),
        )

    def identity(self) -> AgentIdentity:
        return AgentIdentity(
            agent_id=self.agent_id, name=self.name,
            binding_generation=self.binding_generation,
        )


class AgentDirectory:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    @staticmethod
    def _valid_name(name: str) -> bool:
        return (
            1 <= len(name) <= 64 and name[0].islower() and name[0].isalpha()
            and all(char.islower() or char.isdigit() or char in "_.-" for char in name)
        )

    @staticmethod
    def _result(binding: AgentBinding) -> AgentDirectoryResult:
        return AgentDirectoryResult(status="ok", identity=binding.identity())

    async def register(
        self, principal_key: str, session_generation: str, name: str, mailbox_work_id: UUID,
    ) -> AgentDirectoryResult:
        if not principal_key or not session_generation or not self._valid_name(name):
            return AgentDirectoryResult(status="denied", reason="invalid_agent_registration")
        try:
            async with self.engine.begin() as connection:
                await connection.execute(text(
                    "LOCK TABLE agent_bindings IN SHARE ROW EXCLUSIVE MODE"
                ))
                session = (await connection.execute(select(agent_bindings).where(
                    agent_bindings.c.principal_key == principal_key,
                    agent_bindings.c.session_generation == session_generation,
                ))).mappings().one_or_none()
                if session is not None:
                    binding = AgentBinding.from_row(session)
                    if binding.name != name or binding.mailbox_work_id != mailbox_work_id:
                        return AgentDirectoryResult(
                            status="conflict", reason="session_already_registered"
                        )
                    return self._result(binding)
                named = (await connection.execute(select(agent_bindings).where(
                    agent_bindings.c.name == name
                ))).mappings().one_or_none()
                if named is not None:
                    return AgentDirectoryResult(status="conflict", reason="name_unavailable")
                row = (await connection.execute(insert(agent_bindings).values(
                    agent_id=uuid4(), name=name, mailbox_work_id=mailbox_work_id,
                    principal_key=principal_key, session_generation=session_generation,
                    binding_generation=1,
                ).returning(*agent_bindings.c))).mappings().one()
            return self._result(AgentBinding.from_row(row))
        except (SQLAlchemyError, TypeError, ValueError):
            return AgentDirectoryResult(
                status="recovery_required", reason="agent_directory_unavailable"
            )

    async def current(
        self, principal_key: str, session_generation: str,
    ) -> AgentDirectoryResult:
        try:
            async with self.engine.connect() as connection:
                row = (await connection.execute(select(agent_bindings).where(
                    agent_bindings.c.principal_key == principal_key,
                    agent_bindings.c.session_generation == session_generation,
                ))).mappings().one_or_none()
            if row is None:
                return AgentDirectoryResult(status="denied", reason="agent_not_registered")
            return self._result(AgentBinding.from_row(row))
        except (SQLAlchemyError, TypeError, ValueError):
            return AgentDirectoryResult(
                status="recovery_required", reason="agent_directory_unavailable"
            )

    async def resolve(self, name: str) -> AgentBinding | None:
        try:
            async with self.engine.connect() as connection:
                row = (await connection.execute(select(agent_bindings).where(
                    agent_bindings.c.name == name
                ))).mappings().one_or_none()
            return None if row is None else AgentBinding.from_row(row)
        except (SQLAlchemyError, TypeError, ValueError):
            return None

    async def takeover(
        self, name: str, principal_key: str, session_generation: str, *, authorized: bool = False,
    ) -> AgentDirectoryResult:
        if not authorized:
            raise PermissionError("agent takeover requires explicit trusted authorization")
        try:
            async with self.engine.begin() as connection:
                await connection.execute(text(
                    "LOCK TABLE agent_bindings IN SHARE ROW EXCLUSIVE MODE"
                ))
                row = (await connection.execute(select(agent_bindings).where(
                    agent_bindings.c.name == name
                ).with_for_update())).mappings().one_or_none()
                if row is None:
                    return AgentDirectoryResult(status="denied", reason="agent_not_registered")
                binding = AgentBinding.from_row(row)
                duplicate = (await connection.execute(select(agent_bindings.c.agent_id).where(
                    agent_bindings.c.principal_key == principal_key,
                    agent_bindings.c.session_generation == session_generation,
                    agent_bindings.c.agent_id != binding.agent_id,
                ))).one_or_none()
                if duplicate is not None:
                    return AgentDirectoryResult(
                        status="conflict", reason="session_already_registered"
                    )
                updated = (await connection.execute(update(agent_bindings).where(
                    agent_bindings.c.agent_id == binding.agent_id
                ).values(
                    principal_key=principal_key,
                    session_generation=session_generation,
                    binding_generation=binding.binding_generation + 1,
                ).returning(*agent_bindings.c))).mappings().one()
            return self._result(AgentBinding.from_row(updated))
        except (SQLAlchemyError, TypeError, ValueError):
            return AgentDirectoryResult(
                status="recovery_required", reason="agent_directory_unavailable"
            )
