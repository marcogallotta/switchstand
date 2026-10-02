"""Inert compact PostgreSQL repository for canonical current work."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import cast
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from .state import canonical_work, legacy_work_aliases


def normalize_title(value: str) -> str:
    if "\0" in value:
        raise ValueError("title contains a PostgreSQL text null character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("title is not valid UTF-8") from error
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not normalized:
        raise ValueError("title must contain non-whitespace text")
    return normalized


@dataclass(frozen=True)
class CurrentWork:
    work_id: UUID
    title: str
    completed: bool
    notes: str
    row_version: int = 1
    priority: str | None = None
    work_type: str | None = None
    lifecycle_state: str | None = None
    review_next_action: str | None = None
    wait_kind: str | None = None
    unblock_condition: str | None = None
    next_due: str | None = None


_SCALARS = (
    "title", "completed", "notes", "priority", "work_type", "lifecycle_state",
    "review_next_action", "wait_kind", "unblock_condition", "next_due",
)
_COLUMNS = (canonical_work.c.work_id, *[canonical_work.c[name] for name in _SCALARS],
            canonical_work.c.row_version)


def _item(row: Sequence[object]) -> CurrentWork:
    return CurrentWork(
        cast(UUID, row[0]), cast(str, row[1]), cast(bool, row[2]), cast(str, row[3]),
        cast(int, row[11]), cast(str | None, row[4]), cast(str | None, row[5]),
        cast(str | None, row[6]), cast(str | None, row[7]), cast(str | None, row[8]),
        cast(str | None, row[9]), cast(str | None, row[10]),
    )


class CanonicalWorkRepository:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def get(self, work_id: UUID) -> CurrentWork | None:
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(*_COLUMNS).where(
                canonical_work.c.work_id == work_id
            ))).one_or_none()
        return None if row is None else _item(row)

    async def search(
        self, query: str | None = None, *, completed: bool | None = None, limit: int = 100,
    ) -> tuple[CurrentWork, ...]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        statement = select(*_COLUMNS)
        if query is not None:
            statement = statement.where(
                canonical_work.c.normalized_title.contains(normalize_title(query))
            )
        if completed is not None:
            statement = statement.where(canonical_work.c.completed == completed)
        statement = statement.order_by(
            canonical_work.c.normalized_title, canonical_work.c.work_id
        ).limit(limit)
        async with self.engine.connect() as connection:
            return tuple(_item(row) for row in (await connection.execute(statement)).all())

    async def resolve_asana_gid(self, gid: str) -> UUID | None:
        async with self.engine.connect() as connection:
            return await connection.scalar(select(legacy_work_aliases.c.work_id).where(
                legacy_work_aliases.c.asana_task_gid == gid
            ))

    async def bind_asana_gid(self, gid: str, work_id: UUID) -> None:
        if not gid:
            raise ValueError("legacy Asana task GID cannot be empty")
        async with self.engine.begin() as connection:
            await connection.execute(insert(legacy_work_aliases).values(
                asana_task_gid=gid, work_id=work_id
            ))

    async def create(self, item: CurrentWork) -> None:
        if item.row_version != 1:
            raise ValueError("new canonical work must start at version 1")
        async with self.engine.begin() as connection:
            await connection.execute(insert(canonical_work).values(
                work_id=item.work_id, normalized_title=normalize_title(item.title),
                row_version=1, **{name: getattr(item, name) for name in _SCALARS},
            ))

    async def replace(self, item: CurrentWork) -> CurrentWork:
        async with self.engine.begin() as connection:
            current = await connection.scalar(select(canonical_work.c.row_version).where(
                canonical_work.c.work_id == item.work_id
            ).with_for_update())
            if current is None:
                raise LookupError("canonical work does not exist")
            if current != item.row_version:
                raise ValueError("stale canonical work version")
            next_version = current + 1
            await connection.execute(update(canonical_work).where(
                canonical_work.c.work_id == item.work_id
            ).values(
                normalized_title=normalize_title(item.title), row_version=next_version,
                **{name: getattr(item, name) for name in _SCALARS},
            ))
        return replace(item, row_version=next_version)
