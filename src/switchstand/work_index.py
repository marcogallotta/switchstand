"""Stage 1 DB authority for work scalar state and admitted-work search."""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import (
    and_,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from .contracts import Routing, WorkContext, WorkSearchItem, WorkSearchRequest, WorkSearchResult
from .core import Handle, ProviderWork
from .state import work_authority, work_authority_cutovers, work_edges, work_handles, work_index

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

    @staticmethod
    def content_revision(notes: str, context: WorkContext) -> str:
        payload = json.dumps(
            [notes, context.model_dump(mode="json")], sort_keys=True, separators=(",", ":")
        )
        return "content_" + hashlib.sha256(payload.encode()).hexdigest()

    async def revision(
        self, work_id: UUID, provider_revision: str, *, provider_notes: str | None = None,
        provider_context: WorkContext | None = None,
    ) -> str:
        generation = await self.generation()
        if generation is None:
            return provider_revision
        row = await self.get(work_id)
        if row is None:
            raise PermissionError("work is not admitted to the authoritative corpus")
        from .work_metadata import authority_generation

        if await authority_generation(self.engine) is not None and provider_notes is not None:
            provider_revision = self.content_revision(
                provider_notes, provider_context or row.context
            )
        return self._revision(generation, row, provider_revision)

    async def matches(
        self, work_id: UUID, observed: str, provider_revision: str, *,
        provider_notes: str | None = None, provider_context: WorkContext | None = None,
    ) -> bool:
        return observed == await self.revision(
            work_id, provider_revision, provider_notes=provider_notes,
            provider_context=provider_context,
        )

    async def project(self, work_id: UUID, provider: ProviderWork) -> ProviderWork:
        generation = await self.generation()
        if generation is None:
            return provider
        row = await self._refresh(work_id, provider)
        return ProviderWork(
            title=row.title, notes=provider.notes, completed=row.completed,
            revision=self._revision(generation, row, row.provider_revision),
            routing=row.routing, context=provider.context, canonical=provider.canonical,
        )

    async def _refresh(self, work_id: UUID, provider: ProviderWork) -> IndexedWork:
        """Refresh still-provider-owned facts without accepting title or completion."""
        from .work_metadata import authority_generation

        metadata_active = await authority_generation(self.engine) is not None
        provider_revision = (
            self.content_revision(provider.notes, provider.context)
            if metadata_active else provider.revision
        )
        values = {
            "provider_revision": provider_revision,
            "context": provider.context.model_dump(mode="json"),
        }
        if not metadata_active:
            values["routing"] = provider.routing.model_dump(mode="json")
        async with self.engine.begin() as connection:
            current = _indexed((await connection.execute(
                select(*INDEX_COLUMNS).where(work_index.c.work_id == work_id).with_for_update()
            )).one_or_none())
            if current is None:
                raise PermissionError("work is not admitted to the authoritative corpus")
            if (
                current.provider_revision != provider_revision
                or (not metadata_active and current.routing != provider.routing)
                or current.context != provider.context
            ):
                await connection.execute(update(work_index).where(
                    work_index.c.work_id == work_id
                ).values(
                    **values,
                    row_version=current.row_version if metadata_active else current.row_version + 1,
                ))
                current = IndexedWork(
                    work_id, current.title, current.completed, provider_revision,
                    current.row_version if metadata_active else current.row_version + 1,
                    current.routing if metadata_active else provider.routing,
                    provider.context,
                )
        return current

    @staticmethod
    def _check_field_types(fields: dict[str, object], allowed: set[str]) -> None:
        if not fields or not fields.keys() <= allowed:
            raise ValueError("invalid DB-authoritative work fields")
        if (title := fields.get("title")) is not None and not isinstance(title, str):
            raise TypeError("title must be a string")
        if (completed := fields.get("completed")) is not None and not isinstance(completed, bool):
            raise TypeError("completed must be a boolean")

    async def _field_values(
        self, connection: AsyncConnection, current: IndexedWork,
        fields: dict[str, object], *, metadata_active: bool,
    ) -> tuple[dict[str, object], Routing]:
        from .work_metadata import MUTABLE_FIELDS

        title, completed = fields.get("title"), fields.get("completed")
        values: dict[str, object] = {}
        if title is not None and current.title != title:
            values.update(title=title, normalized_title=normalize_title(cast(str, title)))
        if completed is not None and current.completed != completed:
            values["completed"] = completed
        routing = current.routing
        updates = {field: fields[field] for field in fields.keys() & MUTABLE_FIELDS}
        if metadata_active and completed is not None and "lifecycle_state" not in updates:
            updates["lifecycle_state"] = "TERMINAL" if completed else "CURRENT"
            if completed:
                updates.update(wait_kind="NONE", unblock_condition="NONE", next_due="NONE")
        if metadata_active and "lifecycle_state" in updates and completed is None:
            completed = updates["lifecycle_state"] == "TERMINAL"
            values["completed"] = completed
        if updates:
            routing = Routing.model_validate(current.routing.model_dump(mode="json") | updates)
            if routing.canonical_root not in {None, "NONE", "UNKNOWN"}:
                root = UUID(cast(str, routing.canonical_root))
                if (await connection.execute(select(work_index.c.work_id).where(
                    work_index.c.work_id == root
                ))).first() is None:
                    raise ValueError("canonical root is not admitted")
            effective_completed = cast(bool, values.get("completed", current.completed))
            if effective_completed != (routing.lifecycle_state == "TERMINAL"):
                raise ValueError("lifecycle TERMINAL must exactly match completion")
            values["routing"] = routing.model_dump(mode="json")
        return values, routing

    async def validate_update_fields(self, work_id: UUID, fields: dict[str, object]) -> None:
        """Reject deterministic Stage 2 request faults before journaling an effect."""
        from .work_metadata import MUTABLE_FIELDS, authority_generation

        metadata_active = await authority_generation(self.engine) is not None
        allowed: set[str] = {"title", "completed"}
        if metadata_active:
            allowed.update(MUTABLE_FIELDS)
        self._check_field_types(fields, allowed)
        async with self.engine.connect() as connection:
            current = _indexed((await connection.execute(select(*INDEX_COLUMNS).where(
                work_index.c.work_id == work_id
            ))).one_or_none())
            if current is None:
                raise PermissionError("work is not admitted to the authoritative corpus")
            await self._field_values(connection, current, fields, metadata_active=metadata_active)

    async def update_fields(
        self, work_id: UUID, observed: str, provider: ProviderWork,
        fields: dict[str, object],
    ) -> tuple[str, bool]:
        generation = await self.generation()
        if generation is None:
            raise RuntimeError("work authority is not active")
        from .work_metadata import MUTABLE_FIELDS, authority_generation

        metadata_active = await authority_generation(self.engine) is not None
        allowed: set[str] = {"title", "completed"}
        if metadata_active:
            allowed.update(MUTABLE_FIELDS)
        self._check_field_types(fields, allowed)
        title = cast(str | None, fields.get("title"))
        completed = cast(bool | None, fields.get("completed"))
        async with self.engine.begin() as connection:
            current = _indexed((await connection.execute(
                select(*INDEX_COLUMNS).where(work_index.c.work_id == work_id).with_for_update()
            )).one_or_none())
            if current is None:
                raise PermissionError("work is not admitted to the authoritative corpus")
            content_revision = (
                self.content_revision(provider.notes, provider.context)
                if metadata_active else provider.revision
            )
            if observed != self._revision(generation, current, content_revision):
                return self._revision(generation, current, content_revision), False
            values, routing = await self._field_values(
                connection, current, fields, metadata_active=metadata_active
            )
            if values:
                await connection.execute(update(work_index).where(
                    work_index.c.work_id == work_id
                ).values(**values, row_version=current.row_version + 1))
                current = IndexedWork(
                    work_id, title if title is not None else current.title,
                    completed if completed is not None else current.completed,
                    current.provider_revision,
                    current.row_version + 1, routing, current.context,
                )
        return self._revision(generation, current, content_revision), True

    async def admit_created(self, work_id: UUID, title: str, provider: ProviderWork) -> None:
        if not await self.active():
            return
        from .work_metadata import NONE, UNKNOWN, authority_generation

        routing = provider.routing
        if await authority_generation(self.engine) is not None:
            terminal = provider.completed
            routing = Routing(
                lifecycle_state="TERMINAL" if terminal else "UNKNOWN",
                canonical_root=UNKNOWN, owner_key=UNKNOWN,
                wait_kind=NONE if terminal else UNKNOWN,
                unblock_condition=NONE if terminal else UNKNOWN,
                next_due=NONE if terminal else UNKNOWN,
                next_action_class=UNKNOWN, next_action_ref=UNKNOWN,
            )
        values = {
            "work_id": work_id, "title": title, "normalized_title": normalize_title(title),
            "completed": provider.completed, "provider_revision": provider.revision,
            "row_version": 1, "routing": routing.model_dump(mode="json"),
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

    async def update_dependency(
        self, work_id: UUID, target_work_id: UUID, observed: str,
        provider: ProviderWork, *, add: bool,
    ) -> tuple[str, bool]:
        from .work_metadata import authority_generation

        if await authority_generation(self.engine) is None:
            raise RuntimeError("work metadata authority is not active")
        generation = await self.generation()
        if generation is None:
            raise RuntimeError("work authority is not active")
        if work_id == target_work_id:
            raise ValueError("work cannot depend on itself")
        ordered = sorted((work_id, target_work_id), key=lambda value: value.int)
        async with self.engine.begin() as connection:
            locked = (await connection.execute(select(*INDEX_COLUMNS).where(
                work_index.c.work_id.in_(ordered)
            ).order_by(work_index.c.work_id).with_for_update())).all()
            if len(locked) != 2:
                raise PermissionError("dependency target is not admitted")
            by_id = {row[0]: _indexed(row) for row in locked}
            current = by_id[work_id]
            if current is None:
                raise PermissionError("work is not admitted")
            content_revision = self.content_revision(provider.notes, provider.context)
            if observed != self._revision(generation, current, content_revision):
                return self._revision(generation, current, content_revision), False
            predicate = (
                (work_edges.c.work_id == work_id)
                & (work_edges.c.depends_on_work_id == target_work_id)
            )
            exists = (await connection.execute(select(work_edges.c.work_id).where(
                predicate
            ))).first() is not None
            if add and not exists:
                await connection.execute(insert(work_edges).values(
                    work_id=work_id, depends_on_work_id=target_work_id
                ))
            elif not add and exists:
                await connection.execute(delete(work_edges).where(predicate))
            else:
                return self._revision(generation, current, content_revision), True
            await connection.execute(update(work_index).where(
                work_index.c.work_id == work_id
            ).values(row_version=current.row_version + 1))
            current = IndexedWork(
                current.work_id, current.title, current.completed, current.provider_revision,
                current.row_version + 1, current.routing, current.context,
            )
        return self._revision(generation, current, content_revision), True

    async def validate_dependency(self, work_id: UUID, target_work_id: UUID) -> None:
        """Reject deterministic dependency faults before journaling an effect."""
        if work_id == target_work_id:
            raise ValueError("work cannot depend on itself")
        async with self.engine.connect() as connection:
            admitted = set((await connection.execute(select(work_index.c.work_id).where(
                work_index.c.work_id.in_((work_id, target_work_id))
            ))).scalars())
        if admitted != {work_id, target_work_id}:
            raise ValueError("dependency work is not admitted")

    async def dependency_matches(
        self, work_id: UUID, target_work_id: UUID, *, add: bool,
    ) -> bool:
        async with self.engine.connect() as connection:
            exists = (await connection.execute(select(work_edges.c.work_id).where(
                (work_edges.c.work_id == work_id)
                & (work_edges.c.depends_on_work_id == target_work_id)
            ))).first() is not None
        return exists == add

    async def dependencies(self, work_id: UUID) -> tuple[UUID, ...]:
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(
                work_edges.c.depends_on_work_id
            ).where(work_edges.c.work_id == work_id).order_by(
                work_edges.c.depends_on_work_id
            ))).scalars().all()
        return tuple(rows)

    async def blocks(self, work_id: UUID) -> tuple[UUID, ...]:
        """Compute inverse BLOCKS truth; never persist a duplicate edge."""
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(
                work_edges.c.work_id
            ).where(work_edges.c.depends_on_work_id == work_id).order_by(
                work_edges.c.work_id
            ))).scalars().all()
        return tuple(rows)

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


class ActivationNotCommitted(RuntimeError): """Fresh readback proves activation did not apply."""


class ActivationUnknown(RuntimeError): """Activation may have committed; reconcile while gated."""


@dataclass(frozen=True)
class ActivationReceipt:
    count: int
    generation: int
    corpus_digest: str
    edge_digest: str | None = None
    pre_corpus_digest: str = ""
    pre_edge_digest: str | None = None
    recovered_after_commit_error: bool = False


def imported_values(handle: Handle, item: ImportItem) -> dict[str, object]:
    """Build a migration row from a validated ProviderSearchItem-like object."""
    title, routing, context = item.title, item.routing, item.context
    return {
        "work_id": handle.id, "title": title, "normalized_title": normalize_title(title),
        "completed": item.completed,
        "provider_revision": item.revision, "row_version": 1,
        "routing": routing.model_dump(mode="json"), "context": context.model_dump(mode="json"),
    }


def canonical_digest(rows: object) -> str:
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def manifest_digest(items: tuple[ImportItem, ...], handles: dict[str, Handle]) -> str:
    rows: list[list[object]] = []
    for item in sorted(items, key=lambda value: value.provider_work_id):
        handle = handles[item.provider_work_id]
        values = imported_values(handle, item)
        rows.append([
            str(handle.id), item.provider_work_id, values["title"], values["normalized_title"],
            values["completed"], values["provider_revision"], values["row_version"],
            values["routing"], values["context"],
        ])
    return canonical_digest(rows)


async def corpus_digest_from_connection(connection: AsyncConnection) -> str:
    rows = (await connection.execute(select(
        work_index.c.work_id, work_handles.c.provider_work_id, work_index.c.title,
        work_index.c.normalized_title, work_index.c.completed,
        work_index.c.provider_revision, work_index.c.row_version,
        work_index.c.routing, work_index.c.context,
    ).join(work_handles, work_handles.c.id == work_index.c.work_id).order_by(
        work_handles.c.provider_work_id
    ))).all()
    return canonical_digest([
        [str(row[0]), row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8]]
        for row in rows
    ])


async def corpus_digest(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return await corpus_digest_from_connection(connection)


async def prepare_manifest(engine: AsyncEngine, items: tuple[ImportItem, ...]) -> str:
    provider_ids = [item.provider_work_id for item in items]
    if not provider_ids or len(provider_ids) != len(set(provider_ids)):
        raise ValueError("final scan must be non-empty and contain each provider work exactly once")
    async with engine.begin() as connection:
        await connection.execute(select(func.pg_advisory_xact_lock(0x53544731)))
        if (await connection.execute(select(work_authority.c.scope))).first() is not None or (
            await connection.execute(select(work_authority_cutovers.c.scope))).first() is not None:
            raise ActivationUnknown("Stage 1 authority already exists; reconcile its receipt")
        rows = (await connection.execute(select(
            work_handles.c.id, work_handles.c.provider_work_id,
        ).where((work_handles.c.provider == "asana") & work_handles.c.provider_work_id.in_(provider_ids)))).all()
        handles = {row[1]: Handle(row[0], "asana", row[1]) for row in rows}
        for provider_id in provider_ids:
            if provider_id not in handles:
                handle = Handle(uuid4(), "asana", provider_id)
                await connection.execute(insert(work_handles).values(
                    id=handle.id, provider="asana", provider_work_id=provider_id,
                ))
                handles[provider_id] = handle
        return manifest_digest(items, handles)


async def _stage1_markers(engine: AsyncEngine) -> tuple[object, object]:
    async with engine.connect() as connection:
        authority = (await connection.execute(select(
            work_authority.c.state, work_authority.c.generation,
        ).where(work_authority.c.scope == SCOPE))).one_or_none()
        cutover = (await connection.execute(select(
            work_authority_cutovers.c.generation,
        ).where(work_authority_cutovers.c.scope == SCOPE))).one_or_none()
    return authority, cutover


async def _commit(transaction: AsyncTransaction) -> None:
    await transaction.commit()


async def reconcile_activation(engine: AsyncEngine, receipt: ActivationReceipt) -> ActivationReceipt:
    try:
        authority, cutover = await _stage1_markers(engine)
        observed = await corpus_digest(engine)
    except BaseException as error:
        raise ActivationUnknown("Stage 1 activation outcome UNKNOWN; keep the maintenance gate") from error
    if authority == (STATE, receipt.generation) and cutover == (receipt.generation,) and observed == receipt.corpus_digest:
        return replace(receipt, recovered_after_commit_error=True)
    if authority is None and cutover is None and observed == receipt.pre_corpus_digest:
        raise ActivationNotCommitted("Stage 1 readback proves authority was not activated")
    raise ActivationUnknown("Stage 1 activation outcome UNKNOWN; repair forward")


async def activate(
    engine: AsyncEngine, items: tuple[ImportItem, ...], *, expected_manifest_digest: str,
    before_commit: Callable[[ActivationReceipt], None] | None = None,
) -> ActivationReceipt:
    """Atomically bind the final offline scan and flip all Stage 1 authority."""
    provider_ids = [item.provider_work_id for item in items]
    if not provider_ids or len(provider_ids) != len(set(provider_ids)):
        raise ValueError("final scan must be non-empty and contain each provider work exactly once")
    pre_digest = await corpus_digest(engine)
    connection = await engine.connect()
    transaction = await connection.begin()
    commit_started = False
    actual_digest = ""
    receipt = ActivationReceipt(len(items), 1, "", pre_corpus_digest=pre_digest)
    try:
        await connection.execute(select(func.pg_advisory_xact_lock(0x53544731)))
        if (await connection.execute(select(work_authority.c.scope))).first() is not None or (
            await connection.execute(select(work_authority_cutovers.c.scope))).first() is not None:
            raise ActivationUnknown("Stage 1 authority already exists; reconcile its receipt")
        existing_rows = (await connection.execute(select(
            work_handles.c.id, work_handles.c.provider_work_id,
        ).where(
            (work_handles.c.provider == "asana")
            & work_handles.c.provider_work_id.in_(provider_ids)
        ))).all()
        handles = {row[1]: Handle(row[0], "asana", row[1]) for row in existing_rows}
        if set(handles) != set(provider_ids):
            raise ValueError("manifest bindings are not prepared for this final scan")
        actual_digest = manifest_digest(items, handles)
        if actual_digest != expected_manifest_digest:
            raise ValueError("final scan manifest does not match the approved expected digest")
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
        receipt = ActivationReceipt(len(items), 1, actual_digest, pre_corpus_digest=pre_digest)
        if before_commit is not None:
            before_commit(receipt)
        commit_started = True
        await _commit(transaction)
    except BaseException:
        if not commit_started:
            with suppress(BaseException):
                await transaction.rollback()
            raise
        with suppress(BaseException):
            await connection.close()
        return await reconcile_activation(engine, receipt)
    finally:
        with suppress(BaseException):
            await connection.close()
    authority, cutover = await _stage1_markers(engine)
    observed_digest = await corpus_digest(engine)
    if authority != (STATE, 1) or cutover != (1,) or observed_digest != actual_digest:
        raise ActivationUnknown("Stage 1 committed readback is inconsistent; repair forward")
    return receipt
