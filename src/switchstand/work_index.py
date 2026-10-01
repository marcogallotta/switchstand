"""Stage 1 DB authority for work scalar state and admitted-work search."""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import (
    and_,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from .contracts import Routing, WorkContext, WorkSearchItem, WorkSearchRequest, WorkSearchResult
from .core import Handle, ProviderWork
from .state import work_authority, work_authority_cutovers, work_handles, work_index

SCOPE = "workspace"
STATE = "POSTGRES_AUTHORITY"

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
class IndexedWork:
    work_id: UUID
    title: str
    completed: bool
    provider_revision: str
    row_version: int
    routing: Routing
    context: WorkContext


def _indexed(row: Sequence[object] | None) -> IndexedWork | None:
    if row is None:
        return None
    return IndexedWork(
        work_id=cast(UUID, row[0]), title=cast(str, row[1]), completed=cast(bool, row[2]),
        provider_revision=cast(str, row[3]), row_version=cast(int, row[4]),
        routing=Routing.model_validate(row[5]), context=WorkContext.model_validate(row[6]),
    )


INDEX_COLUMNS = (
    work_index.c.work_id, work_index.c.title, work_index.c.completed,
    work_index.c.provider_revision, work_index.c.row_version,
    work_index.c.routing, work_index.c.context,
)


class WorkIndex:
    """The single runtime owner of the monotonic Stage 1 authority boundary."""

    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def generation(self) -> int | None:
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(
                select(work_authority.c.state).where(
                    work_authority.c.scope == SCOPE
                ).scalar_subquery(),
                select(work_authority.c.generation).where(
                    work_authority.c.scope == SCOPE
                ).scalar_subquery(),
                select(work_authority_cutovers.c.generation).where(
                    work_authority_cutovers.c.scope == SCOPE
                ).scalar_subquery(),
            ))).one()
        if row == (None, None, None):
            return None
        if (
            row[0] != STATE or not isinstance(row[1], int) or row[1] < 1
            or not isinstance(row[2], int) or row[2] != row[1]
        ):
            raise ValueError("inconsistent irreversible work authority state")
        return row[1]

    async def active(self) -> bool:
        return await self.generation() is not None

    async def get(self, work_id: UUID) -> IndexedWork | None:
        async with self.engine.connect() as connection:
            row = (await connection.execute(
                select(*INDEX_COLUMNS).where(work_index.c.work_id == work_id)
            )).one_or_none()
        return _indexed(row)

    @staticmethod
    def _revision(generation: int, row: IndexedWork, provider_revision: str) -> str:
        payload = f"{generation}\0{row.work_id}\0{row.row_version}\0{provider_revision}"
        return "s1_" + hashlib.sha256(payload.encode()).hexdigest()

    async def revision(self, work_id: UUID, provider_revision: str) -> str:
        generation = await self.generation()
        if generation is None:
            return provider_revision
        row = await self.get(work_id)
        if row is None:
            raise PermissionError("work is not admitted to the authoritative corpus")
        return self._revision(generation, row, provider_revision)

    async def matches(self, work_id: UUID, observed: str, provider_revision: str) -> bool:
        return observed == await self.revision(work_id, provider_revision)

    async def project(self, work_id: UUID, provider: ProviderWork) -> ProviderWork:
        generation = await self.generation()
        if generation is None:
            return provider
        row = await self._refresh(work_id, provider)
        return ProviderWork(
            title=row.title, notes=provider.notes, completed=row.completed,
            revision=self._revision(generation, row, provider.revision),
            routing=provider.routing, context=provider.context, canonical=provider.canonical,
        )

    async def _refresh(self, work_id: UUID, provider: ProviderWork) -> IndexedWork:
        """Refresh still-provider-owned facts without accepting title or completion."""
        values = {
            "provider_revision": provider.revision,
            "routing": provider.routing.model_dump(mode="json"),
            "context": provider.context.model_dump(mode="json"),
        }
        async with self.engine.begin() as connection:
            current = _indexed((await connection.execute(
                select(*INDEX_COLUMNS).where(work_index.c.work_id == work_id).with_for_update()
            )).one_or_none())
            if current is None:
                raise PermissionError("work is not admitted to the authoritative corpus")
            if (
                current.provider_revision != provider.revision
                or current.routing != provider.routing
                or current.context != provider.context
            ):
                await connection.execute(update(work_index).where(
                    work_index.c.work_id == work_id
                ).values(**values, row_version=current.row_version + 1))
                current = IndexedWork(
                    work_id, current.title, current.completed, provider.revision,
                    current.row_version + 1, provider.routing, provider.context,
                )
        return current

    async def update_fields(
        self, work_id: UUID, observed: str, provider: ProviderWork,
        fields: dict[str, object],
    ) -> tuple[str, bool]:
        generation = await self.generation()
        if generation is None:
            raise RuntimeError("work authority is not active")
        if not fields or not fields.keys() <= {"title", "completed"}:
            raise ValueError("invalid DB-authoritative work fields")
        title = fields.get("title", None)
        completed = fields.get("completed", None)
        if title is not None and not isinstance(title, str):
            raise TypeError("title must be a string")
        if completed is not None and not isinstance(completed, bool):
            raise TypeError("completed must be a boolean")
        async with self.engine.begin() as connection:
            current = _indexed((await connection.execute(
                select(*INDEX_COLUMNS).where(work_index.c.work_id == work_id).with_for_update()
            )).one_or_none())
            if current is None:
                raise PermissionError("work is not admitted to the authoritative corpus")
            if observed != self._revision(generation, current, provider.revision):
                return self._revision(generation, current, provider.revision), False
            values: dict[str, object] = {}
            if title is not None and current.title != title:
                values.update(title=title, normalized_title=normalize_title(title))
            if completed is not None and current.completed != completed:
                values["completed"] = completed
            if values:
                await connection.execute(update(work_index).where(
                    work_index.c.work_id == work_id
                ).values(**values, row_version=current.row_version + 1))
                current = IndexedWork(
                    work_id, title if title is not None else current.title,
                    completed if completed is not None else current.completed,
                    current.provider_revision,
                    current.row_version + 1, current.routing, current.context,
                )
        return self._revision(generation, current, provider.revision), True

    async def admit_created(self, work_id: UUID, title: str, provider: ProviderWork) -> None:
        if not await self.active():
            return
        values = {
            "work_id": work_id, "title": title, "normalized_title": normalize_title(title),
            "completed": provider.completed, "provider_revision": provider.revision,
            "row_version": 1, "routing": provider.routing.model_dump(mode="json"),
            "context": provider.context.model_dump(mode="json"),
        }
        async with self.engine.begin() as connection:
            result = await connection.execute(
                pg_insert(work_index).values(values).on_conflict_do_nothing()
            )
            if result.rowcount == 0:
                current = _indexed((await connection.execute(
                    select(*INDEX_COLUMNS).where(work_index.c.work_id == work_id)
                )).one())
                if current is None or current.title != title:
                    raise ValueError("created work admission conflict")

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
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid work-search cursor") from exc

    async def search(self, request: WorkSearchRequest) -> WorkSearchResult | None:
        generation = await self.generation()
        if generation is None:
            return None
        normalized = normalize_title(request.text) if request.text is not None else None
        criteria = hashlib.sha256(json.dumps(
            [generation, normalized, request.completed], separators=(",", ":")
        ).encode()).hexdigest()
        query = select(*INDEX_COLUMNS, work_index.c.normalized_title).order_by(
            work_index.c.normalized_title, work_index.c.work_id
        ).limit(request.limit + 1)
        if normalized is not None:
            query = query.where(
                func.to_tsvector("simple", work_index.c.normalized_title).op("@@")(
                    func.plainto_tsquery("simple", normalized)
                )
            )
        if request.completed is not None:
            query = query.where(work_index.c.completed == request.completed)
        if request.cursor is not None:
            title, work_id = self._decode_cursor(request.cursor, criteria)
            query = query.where(or_(
                work_index.c.normalized_title > title,
                and_(work_index.c.normalized_title == title, work_index.c.work_id > work_id),
            ))
        async with self.engine.connect() as connection:
            rows = (await connection.execute(query)).all()
        page = rows[:request.limit]
        items = tuple(
            WorkSearchItem(
                id=item.work_id, title=item.title, completed=item.completed,
                revision=self._revision(generation, item, item.provider_revision),
                routing=item.routing, context=item.context,
            )
            for item in (_indexed(row[:7]) for row in page) if item is not None
        )
        next_cursor = None
        if len(rows) > request.limit and page:
            next_cursor = self._cursor(criteria, cast(str, page[-1][7]), cast(UUID, page[-1][0]))
        return WorkSearchResult(status="ok", items=items, next_cursor=next_cursor)


class ImportItem(Protocol):
    @property
    def provider_work_id(self) -> str: ...
    @property
    def title(self) -> str: ...
    @property
    def completed(self) -> bool: ...
    @property
    def revision(self) -> str: ...
    @property
    def routing(self) -> Routing: ...
    @property
    def context(self) -> WorkContext: ...


def imported_values(handle: Handle, item: ImportItem) -> dict[str, object]:
    """Build a migration row from a validated ProviderSearchItem-like object."""
    title, routing, context = item.title, item.routing, item.context
    return {
        "work_id": handle.id, "title": title, "normalized_title": normalize_title(title),
        "completed": item.completed,
        "provider_revision": item.revision, "row_version": 1,
        "routing": routing.model_dump(mode="json"), "context": context.model_dump(mode="json"),
    }


async def activate(engine: AsyncEngine, items: tuple[ImportItem, ...]) -> int:
    """Atomically bind the final offline scan and flip all Stage 1 authority."""
    provider_ids = [item.provider_work_id for item in items]
    if not provider_ids or len(provider_ids) != len(set(provider_ids)):
        raise ValueError("final scan must be non-empty and contain each provider work exactly once")
    async with engine.begin() as connection:
        await connection.execute(select(func.pg_advisory_xact_lock(0x53544731)))
        if (await connection.execute(select(work_authority.c.scope))).first() is not None:
            raise RuntimeError("POSTGRES_AUTHORITY is already active")
        existing_rows = (await connection.execute(select(
            work_handles.c.id, work_handles.c.provider_work_id,
        ).where(
            (work_handles.c.provider == "asana")
            & work_handles.c.provider_work_id.in_(provider_ids)
        ))).all()
        handles = {row[1]: Handle(row[0], "asana", row[1]) for row in existing_rows}
        for provider_id in provider_ids:
            if provider_id not in handles:
                handle = Handle(uuid4(), "asana", provider_id)
                await connection.execute(insert(work_handles).values(
                    id=handle.id, provider=handle.provider,
                    provider_work_id=handle.provider_work_id,
                ))
                handles[provider_id] = handle
        await connection.execute(insert(work_index), [
            imported_values(handles[provider_id], item)
            for provider_id, item in zip(provider_ids, items, strict=True)
        ])
        await connection.execute(insert(work_authority).values(
            scope=SCOPE, state=STATE, generation=1,
        ))
        await connection.execute(insert(work_authority_cutovers).values(
            scope=SCOPE, generation=1,
        ))
    return len(items)
