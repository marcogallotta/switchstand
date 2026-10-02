"""Inert compact PostgreSQL repository for canonical current work."""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import cast
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    MetaData,
    Table,
    Text,
    and_,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

canonical_metadata = MetaData()
canonical_work = Table(
    "canonical_work", canonical_metadata,
    Column("work_id", PGUUID(as_uuid=True), primary_key=True),
    Column("title", Text, nullable=False),
    Column("normalized_title", Text, nullable=False),
    Column("completed", Boolean, nullable=False),
    Column("notes", Text, nullable=False),
    Column("assignee", Text),
    Column("priority", Text),
    Column("work_type", Text),
    Column("lifecycle_state", Text),
    Column("review_next_action", Text),
    Column("wait_kind", Text),
    Column("unblock_condition", Text),
    Column("next_due", Text),
    Column("row_version", BigInteger, nullable=False),
    CheckConstraint("title <> ''", name="ck_canonical_work_title"),
    CheckConstraint("normalized_title <> ''", name="ck_canonical_work_normalized_title"),
    CheckConstraint("row_version >= 1", name="ck_canonical_work_version"),
)
legacy_work_aliases = Table(
    "legacy_work_aliases", canonical_metadata,
    Column("asana_task_gid", Text, primary_key=True),
    Column("work_id", PGUUID(as_uuid=True),
           ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), nullable=False, index=True),
    CheckConstraint("asana_task_gid <> ''", name="ck_legacy_work_alias_gid"),
)
Index("ix_canonical_work_page", canonical_work.c.normalized_title, canonical_work.c.work_id)


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


def canonical_revision(work_id: UUID, row_version: int) -> str:
    """Return the provider-neutral opaque revision for one canonical work row."""
    if row_version < 1:
        raise ValueError("canonical row version must be positive")
    value = f"{work_id}\0{row_version}".encode()
    return "pg_" + hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class CurrentWork:
    work_id: UUID
    title: str
    completed: bool
    notes: str
    row_version: int = 1
    assignee: str | None = None
    priority: str | None = None
    work_type: str | None = None
    lifecycle_state: str | None = None
    review_next_action: str | None = None
    wait_kind: str | None = None
    unblock_condition: str | None = None
    next_due: str | None = None


@dataclass(frozen=True)
class CurrentWorkPage:
    items: tuple[CurrentWork, ...]
    next_cursor: str | None = None


_SCALARS = (
    "title", "completed", "notes", "assignee", "priority", "work_type", "lifecycle_state",
    "review_next_action", "wait_kind", "unblock_condition", "next_due",
)
_COLUMNS = (canonical_work.c.work_id, *[canonical_work.c[name] for name in _SCALARS],
            canonical_work.c.row_version)


def _item(row: Sequence[object]) -> CurrentWork:
    return CurrentWork(
        cast(UUID, row[0]), cast(str, row[1]), cast(bool, row[2]), cast(str, row[3]),
        cast(int, row[12]), cast(str | None, row[4]), cast(str | None, row[5]),
        cast(str | None, row[6]), cast(str | None, row[7]), cast(str | None, row[8]),
        cast(str | None, row[9]), cast(str | None, row[10]), cast(str | None, row[11]),
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

    async def get_locked(
        self, connection: AsyncConnection, work_id: UUID,
    ) -> CurrentWork | None:
        row = (await connection.execute(select(*_COLUMNS).where(
            canonical_work.c.work_id == work_id
        ).with_for_update())).one_or_none()
        return None if row is None else _item(row)

    async def search(
        self, query: str | None = None, *, completed: bool | None = None,
        cursor: str | None = None, limit: int = 100,
    ) -> CurrentWorkPage:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        normalized = normalize_title(query) if query is not None else None
        criteria = hashlib.sha256(json.dumps(
            [normalized, completed], separators=(",", ":")
        ).encode()).hexdigest()
        statement = select(*_COLUMNS, canonical_work.c.normalized_title)
        if normalized is not None:
            statement = statement.where(
                canonical_work.c.normalized_title.contains(normalized)
            )
        if completed is not None:
            statement = statement.where(canonical_work.c.completed == completed)
        if cursor is not None:
            title, work_id = self._decode_cursor(cursor, criteria)
            statement = statement.where(or_(
                canonical_work.c.normalized_title > title,
                and_(
                    canonical_work.c.normalized_title == title,
                    canonical_work.c.work_id > work_id,
                ),
            ))
        statement = statement.order_by(
            canonical_work.c.normalized_title, canonical_work.c.work_id
        ).limit(limit + 1)
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement)).all()
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit and page:
            next_cursor = self._cursor(
                criteria, cast(str, page[-1][13]), cast(UUID, page[-1][0])
            )
        return CurrentWorkPage(tuple(_item(row[:13]) for row in page), next_cursor)

    @staticmethod
    def _cursor(criteria: str, title: str, work_id: UUID) -> str:
        value = json.dumps([criteria, title, str(work_id)], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str, criteria: str) -> tuple[str, UUID]:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            decoded = cast(object, json.loads(raw))
            if not isinstance(decoded, list):
                raise TypeError
            value = cast(list[object], decoded)
            if (
                len(value) != 3 or value[0] != criteria
                or not isinstance(value[1], str) or not isinstance(value[2], str)
            ):
                raise ValueError
            return value[1], UUID(value[2])
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("invalid canonical work cursor") from error

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
            current = await self.get_locked(connection, item.work_id)
            if current is None or current.row_version != item.row_version:
                raise (LookupError("canonical work does not exist") if current is None
                       else ValueError("stale canonical work version"))
            return await self.replace_locked(connection, item)

    async def replace_locked(
        self, connection: AsyncConnection, item: CurrentWork,
    ) -> CurrentWork:
        next_version = item.row_version + 1
        await connection.execute(update(canonical_work).where(
            canonical_work.c.work_id == item.work_id
        ).values(
            normalized_title=normalize_title(item.title), row_version=next_version,
            **{name: getattr(item, name) for name in _SCALARS},
        ))
        return replace(item, row_version=next_version)
