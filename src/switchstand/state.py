from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from sqlalchemy import Column, MetaData, Row, Table, Text, UniqueConstraint, select
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
columns = (work_handles.c.id, work_handles.c.provider, work_handles.c.provider_work_id)

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

    async def bind(self, provider: str, provider_work_id: str) -> Handle:
        values = {"id": uuid4(), "provider": provider, "provider_work_id": provider_work_id}
        statement = insert(work_handles).values(values)
        statement = statement.on_conflict_do_update(
            index_elements=[work_handles.c.provider, work_handles.c.provider_work_id],
            set_={"provider": provider},
        ).returning(*columns)
        async with self.engine.begin() as connection:
            row = (await connection.execute(statement)).one()
        return Handle(row[0], row[1], row[2])

    @asynccontextmanager
    async def locked(self, work_id: UUID) -> AsyncGenerator[Handle | None]:
        async with self.engine.begin() as connection:
            statement = select(*columns).where(work_handles.c.id == work_id).with_for_update()
            yield _handle((await connection.execute(statement)).one_or_none())
