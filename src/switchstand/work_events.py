"""DB-native work history with stable identities and append idempotency."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncEngine

from .state import canonical_work, work_events


@dataclass(frozen=True)
class StoredWorkEvent:
    id: UUID
    work_id: UUID
    sequence: int
    subtype: str
    text: str | None
    created_at: datetime
    actor: str | None
    asana_story_gid: str | None
    operation_id: UUID | None


@dataclass(frozen=True)
class WorkEventPage:
    events: tuple[StoredWorkEvent, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class AppendOutcome:
    event: StoredWorkEvent
    row_version: int
    created: bool


class UnknownWorkError(ValueError):
    pass


class StaleWorkVersion(ValueError):
    def __init__(self, current: int):
        self.current = current
        super().__init__("observed work version is stale")


class OperationConflictError(ValueError):
    pass


def _event(row: Row[tuple[object, ...]]) -> StoredWorkEvent:
    return StoredWorkEvent(
        id=cast(UUID, row[0]), work_id=cast(UUID, row[1]), sequence=cast(int, row[2]),
        subtype=cast(str, row[3]), text=cast(str | None, row[4]),
        created_at=cast(datetime, row[5]), actor=cast(str | None, row[6]),
        asana_story_gid=cast(str | None, row[7]), operation_id=cast(UUID | None, row[8]),
    )


def _encode_cursor(work_id: UUID, sequence: int) -> str:
    payload = json.dumps([1, str(work_id), sequence], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str, work_id: UUID) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        decoded = cast(object, json.loads(raw))
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("invalid work history cursor") from error
    if (
        not isinstance(decoded, list) or len(cast(list[object], decoded)) != 3
    ):
        raise ValueError("invalid work history cursor")
    value = cast(list[object], decoded)
    if (
        value[0] != 1 or value[1] != str(work_id)
        or not isinstance(value[2], int) or value[2] < 1
    ):
        raise ValueError("invalid work history cursor")
    return value[2]


class WorkEventRepository:
    """Persistence owner for one work's ordered event stream."""

    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self.events = work_events
        self.works = canonical_work
        self.columns = (
            work_events.c.id, work_events.c.work_id, work_events.c.sequence,
            work_events.c.subtype, work_events.c.text, work_events.c.created_at,
            work_events.c.actor, work_events.c.asana_story_gid, work_events.c.operation_id,
        )

    async def page(
        self, work_id: UUID, *, cursor: str | None = None, limit: int = 50,
    ) -> WorkEventPage:
        if not 1 <= limit <= 100:
            raise ValueError("history limit must be between 1 and 100")
        after = 0 if cursor is None else _decode_cursor(cursor, work_id)
        query = (
            select(*self.columns)
            .where((self.events.c.work_id == work_id) & (self.events.c.sequence > after))
            .order_by(self.events.c.sequence)
            .limit(limit + 1)
        )
        async with self.engine.connect() as connection:
            rows = (await connection.execute(query)).all()
        page_rows = rows[:limit]
        events = tuple(_event(row) for row in page_rows)
        next_cursor = (
            _encode_cursor(work_id, events[-1].sequence) if len(rows) > limit else None
        )
        return WorkEventPage(events, next_cursor)

    async def get(self, work_id: UUID, event_id: UUID) -> StoredWorkEvent | None:
        query = select(*self.columns).where(
            (self.events.c.work_id == work_id) & (self.events.c.id == event_id)
        )
        async with self.engine.connect() as connection:
            row = (await connection.execute(query)).one_or_none()
        return None if row is None else _event(row)

    async def append(
        self, work_id: UUID, *, observed_version: int, operation_id: UUID,
        subtype: str, text: str | None, created_at: datetime, actor: str | None = None,
        asana_story_gid: str | None = None,
    ) -> AppendOutcome:
        if not subtype or "\0" in subtype:
            raise ValueError("event subtype must be non-empty PostgreSQL text")
        if text is not None and "\0" in text:
            raise ValueError("event text contains a PostgreSQL text null character")
        if created_at.utcoffset() is None:
            raise ValueError("event creation time must include a timezone")
        if asana_story_gid is not None and not asana_story_gid:
            raise ValueError("historical story GID must be non-empty")
        async with self.engine.begin() as connection:
            version = await connection.scalar(select(self.works.c.row_version).where(
                self.works.c.work_id == work_id
            ).with_for_update())
            if version is None:
                raise UnknownWorkError("work does not exist")

            replay = (await connection.execute(select(*self.columns).where(
                self.events.c.operation_id == operation_id
            ))).one_or_none()
            if replay is not None:
                event = _event(replay)
                if (
                    event.work_id != work_id or event.subtype != subtype or event.text != text
                    or event.created_at != created_at or event.actor != actor
                    or event.asana_story_gid != asana_story_gid
                ):
                    raise OperationConflictError("OperationId was reused with another append")
                return AppendOutcome(event, cast(int, version), False)

            if version != observed_version:
                raise StaleWorkVersion(cast(int, version))
            sequence = cast(int, await connection.scalar(select(
                func.coalesce(func.max(self.events.c.sequence), 0) + 1
            ).where(self.events.c.work_id == work_id)))
            event_id = uuid4()
            values = {
                "id": event_id, "work_id": work_id, "sequence": sequence,
                "subtype": subtype, "text": text, "created_at": created_at, "actor": actor,
                "asana_story_gid": asana_story_gid, "operation_id": operation_id,
            }
            row = (await connection.execute(
                insert(self.events).values(values).returning(*self.columns)
            )).one()
            next_version = cast(int, version) + 1
            await connection.execute(update(self.works).where(
                self.works.c.work_id == work_id
            ).values(row_version=next_version))
            return AppendOutcome(_event(row), next_version, True)
