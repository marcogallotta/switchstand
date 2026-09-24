from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import Column, ForeignKey, MetaData, Row, Table, Text, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from .core import Handle

metadata = MetaData()
work_handles = Table(
    "work_handles",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column("provider", Text, nullable=False), Column("provider_work_id", Text, nullable=False),
    UniqueConstraint("provider", "provider_work_id"),
)
work_event_handles = Table(
    "work_event_handles",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column("work_id", PGUUID(as_uuid=True), ForeignKey("work_handles.id", ondelete="RESTRICT"),
           nullable=False, index=True),
    Column("provider", Text, nullable=False),
    Column("provider_work_id", Text, nullable=False),
    Column("provider_event_id", Text, nullable=False),
    UniqueConstraint("work_id", "provider", "provider_work_id", "provider_event_id"),
)
columns = (work_handles.c.id, work_handles.c.provider, work_handles.c.provider_work_id)
event_columns = (
    work_event_handles.c.id, work_event_handles.c.work_id, work_event_handles.c.provider,
    work_event_handles.c.provider_work_id, work_event_handles.c.provider_event_id,
)


@dataclass(frozen=True)
class EventHandle:
    id: UUID
    work_id: UUID
    provider: str
    provider_work_id: str
    provider_event_id: str

def _handle(row: Row[tuple[UUID, str, str]] | None) -> Handle | None:
    return None if row is None else Handle(row[0], row[1], row[2])


class PostgresState:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine
    async def get(self, work_id: UUID) -> Handle | None:
        async with self.engine.connect() as connection:
            query = select(*columns).where(work_handles.c.id == work_id)
            row = (await connection.execute(query)).one_or_none()
        return _handle(row)

    async def get_by_provider(self, provider: str, provider_work_id: str) -> Handle | None:
        async with self.engine.connect() as connection:
            query = select(*columns).where(
                (work_handles.c.provider == provider)
                & (work_handles.c.provider_work_id == provider_work_id)
            )
            row = (await connection.execute(query)).one_or_none()
        return _handle(row)

    async def bound_provider_ids(self, provider: str) -> frozenset[str]:
        async with self.engine.connect() as connection:
            query = select(work_handles.c.provider_work_id).where(work_handles.c.provider == provider)
            rows = (await connection.execute(query)).scalars()
        return frozenset(rows)

    async def bind(self, provider: str, provider_work_id: str) -> Handle:
        return await self.bind_reserved(uuid4(), provider, provider_work_id, allow_existing=True)

    async def bind_reserved(
        self, work_id: UUID, provider: str, provider_work_id: str, *, allow_existing: bool = False,
    ) -> Handle:
        values = {"id": work_id, "provider": provider, "provider_work_id": provider_work_id}
        statement = insert(work_handles).values(values).on_conflict_do_nothing().returning(*columns)
        async with self.engine.begin() as connection:
            row = (await connection.execute(statement)).one_or_none()
            if row is not None:
                return Handle(row[0], row[1], row[2])
            existing = (await connection.execute(select(*columns).where(
                (work_handles.c.id == work_id)
                | ((work_handles.c.provider == provider)
                   & (work_handles.c.provider_work_id == provider_work_id))
            ))).all()
        if len(existing) != 1:
            raise ValueError("work binding conflict")
        handle = Handle(existing[0][0], existing[0][1], existing[0][2])
        if handle.provider != provider or handle.provider_work_id != provider_work_id:
            raise ValueError("work binding conflict")
        if handle.id != work_id and not allow_existing:
            raise ValueError("reserved WorkId already bound differently")
        return handle

    async def bind_event(
        self, work_id: UUID, provider: str, provider_work_id: str, provider_event_id: str,
    ) -> EventHandle:
        if not provider or not provider_work_id or not provider_event_id:
            raise ValueError("event binding values must be non-empty")
        values = {
            "id": uuid4(), "work_id": work_id, "provider": provider,
            "provider_work_id": provider_work_id, "provider_event_id": provider_event_id,
        }
        async with self.engine.begin() as connection:
            bound = (await connection.execute(select(*columns).where(
                work_handles.c.id == work_id
            ).with_for_update())).one_or_none()
            handle = _handle(bound)
            if (handle is None or handle.provider != provider
                    or handle.provider_work_id != provider_work_id):
                raise ValueError("event target does not match work binding")
            row = (await connection.execute(insert(work_event_handles).values(
                values
            ).on_conflict_do_nothing().returning(*event_columns))).one_or_none()
            if row is None:
                row = (await connection.execute(select(*event_columns).where(
                    (work_event_handles.c.work_id == work_id)
                    & (work_event_handles.c.provider == provider)
                    & (work_event_handles.c.provider_work_id == provider_work_id)
                    & (work_event_handles.c.provider_event_id == provider_event_id)
                ))).one()
        return EventHandle(row[0], row[1], row[2], row[3], row[4])

    async def get_event(self, work_id: UUID, event_id: UUID) -> EventHandle | None:
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(*event_columns).where(
                (work_event_handles.c.id == event_id)
                & (work_event_handles.c.work_id == work_id)
            ))).one_or_none()
        return None if row is None else EventHandle(row[0], row[1], row[2], row[3], row[4])

    @asynccontextmanager
    async def locked(self, work_id: UUID) -> AsyncGenerator[Handle | None]:
        async with self.engine.begin() as connection:
            statement = select(*columns).where(work_handles.c.id == work_id).with_for_update()
            yield _handle((await connection.execute(statement)).one_or_none())
