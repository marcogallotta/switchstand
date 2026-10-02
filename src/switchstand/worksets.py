"""Inert Stage 3 workset snapshot validation and staging."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import cast
from uuid import UUID

from sqlalchemy import Table, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from .contracts import Routing, WorkContext, WorkSearchItem
from .core import ProviderWork
from .discovery import DiscoveredStructure
from .state import (
    work_authority,
    work_authority_cutovers,
    work_edges,
    work_handles,
    work_index,
    work_metadata_authority,
    work_metadata_cutovers,
    work_parent_edges,
    workset_authority,
    workset_cutovers,
    workset_memberships,
    worksets,
)
from .work_index import (
    INDEX_COLUMNS,
    ActivationNotCommitted,
    ActivationReceipt,
    ActivationUnknown,
    IndexedWork,
    WorkIndex,
    canonical_digest,
)

SCOPE = "workspace"
AUTHORITY = "POSTGRES_AUTHORITY"


def _row(*values: object) -> list[object]:
    return list(values)


@dataclass(frozen=True)
class Workset:
    workset_id: UUID
    workset_key: str
    name: str
    kind: str
    role_identity: str | None = None
    state: str = "ACTIVE"
    row_version: int = 1


@dataclass(frozen=True)
class Membership:
    workset_id: UUID
    work_id: UUID
    semantics: str
    member_role: str = "MEMBER"
    row_version: int = 1


@dataclass(frozen=True)
class ParentEdge:
    child_work_id: UUID
    parent_work_id: UUID
    row_version: int = 1


@dataclass(frozen=True)
class WorksetSnapshot:
    worksets: tuple[Workset, ...]
    memberships: tuple[Membership, ...]
    parent_edges: tuple[ParentEdge, ...]

    def validated(self) -> WorksetSnapshot:
        if not self.worksets:
            raise ValueError("workset snapshot must not be empty")
        role_sets = {row.workset_id for row in self.worksets if row.role_identity is not None}
        masters = {row.workset_id for row in self.memberships if row.member_role == "MASTER"}
        if role_sets != masters:
            raise ValueError("durable role worksets require exactly one master")
        parents: dict[UUID, UUID] = {}
        for row in self.parent_edges:
            if row.child_work_id == row.parent_work_id or row.child_work_id in parents:
                raise ValueError("each child must have at most one distinct parent")
            parents[row.child_work_id] = row.parent_work_id
        for child in parents:
            seen: set[UUID] = set()
            current = child
            while current in parents:
                if current in seen:
                    raise ValueError("parent structure contains a cycle")
                seen.add(current)
                current = parents[current]
        return self

    def rows(self) -> list[list[object]]:
        sets = [_row("workset", str(row.workset_id), row.workset_key,
            row.name, row.kind, row.role_identity, row.state, row.row_version)
            for row in sorted(self.worksets, key=lambda item: item.workset_id.int)]
        members = [_row("membership", str(row.workset_id), str(row.work_id),
            row.semantics, row.member_role, row.row_version) for row in sorted(
                self.memberships, key=lambda item: (item.workset_id.int, item.work_id.int))]
        parents = [_row("parent", str(row.child_work_id),
            str(row.parent_work_id), row.row_version) for row in sorted(
                self.parent_edges, key=lambda item: item.child_work_id.int)]
        return sets + members + parents


@dataclass(frozen=True)
class WorksetMember:
    item: WorkSearchItem
    semantics: str
    member_role: str
    depends_on: tuple[UUID, ...]


@dataclass(frozen=True)
class WorksetView:
    revision: str
    workset: Workset
    members: tuple[WorksetMember, ...]


class WorksetReader:
    """Default-off Stage 3 reads; inconsistent irreversible state fails closed."""

    def __init__(self, engine: AsyncEngine):
        self.engine, self.index = engine, WorkIndex(engine)

    @staticmethod
    async def _generation(connection: AsyncConnection) -> tuple[int, int] | None:
        row = (await connection.execute(select(
            select(work_authority.c.state).where(
                work_authority.c.scope == SCOPE).scalar_subquery(),
            select(work_authority.c.generation).where(
                work_authority.c.scope == SCOPE).scalar_subquery(),
            select(work_authority_cutovers.c.generation).where(
                work_authority_cutovers.c.scope == SCOPE).scalar_subquery(),
            select(work_metadata_authority.c.state).where(
                work_metadata_authority.c.scope == SCOPE).scalar_subquery(),
            select(work_metadata_authority.c.generation).where(
                work_metadata_authority.c.scope == SCOPE).scalar_subquery(),
            select(work_metadata_cutovers.c.generation).where(
                work_metadata_cutovers.c.scope == SCOPE).scalar_subquery(),
            select(workset_authority.c.state).where(
                workset_authority.c.scope == SCOPE).scalar_subquery(),
            select(workset_authority.c.generation).where(
                workset_authority.c.scope == SCOPE).scalar_subquery(),
            select(workset_cutovers.c.generation).where(
                workset_cutovers.c.scope == SCOPE).scalar_subquery(),
        ))).one()
        if row[6:] == (None, None, None):
            return None
        triples = (row[0:3], row[3:6], row[6:9])
        if any(
            state != AUTHORITY or generation != receipt
            or not isinstance(generation, int) or generation < 1
            for state, generation, receipt in triples
        ):
            raise ValueError("inconsistent irreversible workset authority state")
        return cast(int, row[7]), cast(int, row[1])

    async def generation(self) -> int | None:
        async with self.engine.connect() as connection:
            generations = await self._generation(connection)
        return None if generations is None else generations[0]

    @staticmethod
    def _workset(row: Sequence[object]) -> Workset:
        return Workset(
            cast(UUID, row[0]), cast(str, row[1]), cast(str, row[2]), cast(str, row[3]),
            cast(str | None, row[4]), cast(str, row[5]), cast(int, row[6]),
        )

    def _item(self, row: Sequence[object], generation: int) -> WorkSearchItem:
        indexed = IndexedWork(
            cast(UUID, row[0]), cast(str, row[1]), cast(bool, row[2]),
            cast(str, row[3]), cast(int, row[4]), Routing.model_validate(row[5]),
            WorkContext.model_validate(row[6]),
        )
        return WorkSearchItem(
            id=indexed.work_id, title=indexed.title, completed=indexed.completed,
            revision=self.index.item_revision(generation, indexed),
            routing=indexed.routing, context=indexed.context,
        )

    async def enumerate(
        self, workset_id: UUID | None = None, *, workset_key: str | None = None,
    ) -> WorksetView | None:
        """Return one explicit workset from a stable PostgreSQL snapshot."""
        if (workset_id is None) == (workset_key is None):
            raise ValueError("workset query requires exactly one ID or key")
        async with self.engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ"
            )
            async with connection.begin():
                generations = await self._generation(connection)
                if generations is None:
                    return None
                generation, index_generation = generations
                identity = (
                    worksets.c.workset_id == workset_id
                    if workset_id is not None else worksets.c.workset_key == workset_key
                )
                set_row = (await connection.execute(select(
                    worksets.c.workset_id, worksets.c.workset_key, worksets.c.name,
                    worksets.c.kind, worksets.c.role_identity, worksets.c.state,
                    worksets.c.row_version,
                ).where(identity))).one_or_none()
                if set_row is None:
                    raise PermissionError("workset is not admitted")
                selected_id = cast(UUID, set_row[0])
                rows = (await connection.execute(select(
                    *INDEX_COLUMNS, workset_memberships.c.semantics,
                    workset_memberships.c.member_role, workset_memberships.c.row_version,
                ).join(workset_memberships,
                       workset_memberships.c.work_id == work_index.c.work_id).where(
                    workset_memberships.c.workset_id == selected_id,
                    work_index.c.completed.is_(False),
                ).order_by(work_index.c.normalized_title, work_index.c.work_id))).all()
                membership_versions = (await connection.execute(select(
                    workset_memberships.c.work_id,
                    workset_memberships.c.semantics,
                    workset_memberships.c.member_role,
                    workset_memberships.c.row_version,
                ).where(
                    workset_memberships.c.workset_id == selected_id,
                ).order_by(workset_memberships.c.work_id))).all()
                member_ids = tuple(row[0] for row in rows)
                edges = (await connection.execute(select(
                    work_edges.c.work_id, work_edges.c.depends_on_work_id,
                ).where(work_edges.c.work_id.in_(member_ids)).order_by(
                    work_edges.c.work_id, work_edges.c.depends_on_work_id,
                ))).all() if member_ids else ()
                revision = "s3w_" + canonical_digest([
                    [generation, *[str(value) if isinstance(value, UUID) else value
                                    for value in set_row]],
                    *[[str(value) if isinstance(value, UUID) else value for value in row]
                      for row in rows],
                    *[[str(value) if isinstance(value, UUID) else value for value in row]
                      for row in membership_versions],
                    *[[str(value) if isinstance(value, UUID) else value for value in edge]
                      for edge in edges],
                ])
        dependencies: dict[UUID, list[UUID]] = {}
        for owner, dependency in edges:
            dependencies.setdefault(owner, []).append(dependency)
        return WorksetView(revision, self._workset(set_row), tuple(
            WorksetMember(
                self._item(row[:7], index_generation), row[7], row[8],
                tuple(dependencies.get(row[0], ())),
            ) for row in rows
        ))

    async def current(self, work_id: UUID) -> WorksetView | None:
        """Resolve an item's authoritative workset without provider discovery."""
        if await self.generation() is None:
            return None
        async with self.engine.connect() as connection:
            workset_id = await connection.scalar(select(
                workset_memberships.c.workset_id).where(
                workset_memberships.c.work_id == work_id,
                workset_memberships.c.semantics == "AUTHORITATIVE",
            ))
        if workset_id is None:
            raise ValueError("admitted work lacks an authoritative workset")
        return await self.enumerate(workset_id=workset_id)

    async def content_authorization(self, work_id: UUID) -> str | None:
        """Return the opaque Stage 3 authorization token, or pre-authority ``None``."""
        generation = await self.generation()
        if generation is None:
            return None
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(
                workset_memberships.c.workset_id,
                workset_memberships.c.row_version,
                worksets.c.row_version,
                worksets.c.state,
            ).join(worksets, worksets.c.workset_id == workset_memberships.c.workset_id).where(
                workset_memberships.c.work_id == work_id,
                workset_memberships.c.semantics == "AUTHORITATIVE",
            ))).one_or_none()
        if row is None:
            raise PermissionError("work lacks authoritative workset membership")
        payload = f"{generation}\0{work_id}\0{row[0]}\0{row[1]}\0{row[2]}\0{row[3]}"
        return "s3_" + hashlib.sha256(payload.encode()).hexdigest()

    async def structure(self, work_id: UUID) -> DiscoveredStructure | None:
        """Read parent/children from Postgres only after Stage 3 authority."""
        if await self.generation() is None:
            return None
        index_generation = await self.index.generation()
        if index_generation is None:
            raise ValueError("Stage 3 authority requires Stage 1 authority")
        async with self.engine.connect() as connection:
            admitted = await connection.scalar(select(workset_memberships.c.work_id).where(
                workset_memberships.c.work_id == work_id,
                workset_memberships.c.semantics == "AUTHORITATIVE",
            ))
            if admitted is None:
                raise ValueError("admitted work lacks an authoritative workset")
            parent_id = await connection.scalar(select(
                work_parent_edges.c.parent_work_id).where(
                work_parent_edges.c.child_work_id == work_id))
            parent = None
            if parent_id is not None:
                row = (await connection.execute(select(*INDEX_COLUMNS).where(
                    work_index.c.work_id == parent_id))).one()
                parent = self._item(row, index_generation)
            child_rows = (await connection.execute(select(*INDEX_COLUMNS).join(
                work_parent_edges,
                work_parent_edges.c.child_work_id == work_index.c.work_id,
            ).where(work_parent_edges.c.parent_work_id == work_id).order_by(
                work_index.c.normalized_title, work_index.c.work_id,
            ))).all()
        return DiscoveredStructure(
            status="ok", revision=str(await self.generation()), parent=parent,
            children=tuple(self._item(row, index_generation) for row in child_rows),
        )

    async def validate_parent(self, work_id: UUID, parent_id: UUID | None) -> None:
        """Reject deterministic parent faults before journaling an effect."""
        if await self.generation() is None:
            raise RuntimeError("workset authority is not active")
        if parent_id == work_id:
            raise ValueError("work cannot parent itself")
        async with self.engine.connect() as connection:
            required = {work_id} if parent_id is None else {work_id, parent_id}
            admitted = set((await connection.execute(select(
                workset_memberships.c.work_id,
            ).where(
                workset_memberships.c.work_id.in_(required),
                workset_memberships.c.semantics == "AUTHORITATIVE",
            ))).scalars())
            if admitted != required:
                raise ValueError("parent work is not admitted")
            edges = (await connection.execute(select(
                work_parent_edges.c.child_work_id, work_parent_edges.c.parent_work_id,
            ))).all()
        self._validate_parent_graph(edges, work_id, parent_id)

    async def validate_create_parent(self, work_id: UUID) -> None:
        """Require an active authoritative workset before a provider child create."""
        if await self.generation() is None:
            raise RuntimeError("workset authority is not active")
        async with self.engine.connect() as connection:
            state = await connection.scalar(select(worksets.c.state).join(
                workset_memberships,
                workset_memberships.c.workset_id == worksets.c.workset_id,
            ).where(
                workset_memberships.c.work_id == work_id,
                workset_memberships.c.semantics == "AUTHORITATIVE",
            ))
        if state != "ACTIVE":
            raise PermissionError("create parent workset is not active")

    @staticmethod
    def _validate_parent_graph(
        edges: Sequence[Sequence[UUID]], work_id: UUID, parent_id: UUID | None,
    ) -> None:
        parents = {row[0]: row[1] for row in edges if row[0] != work_id}
        if parent_id is not None:
            parents[work_id] = parent_id
        current, seen = work_id, set[UUID]()
        while current in parents:
            if current in seen:
                raise ValueError("parent structure contains a cycle")
            seen.add(current)
            current = parents[current]

    async def update_parent(
        self, work_id: UUID, parent_id: UUID | None, expected_row_version: int,
    ) -> bool:
        """Atomically replace one parent edge and advance the owning work revision."""
        if await self.generation() is None:
            raise RuntimeError("workset authority is not active")
        required = {work_id} if parent_id is None else {work_id, parent_id}
        async with self.engine.begin() as connection:
            await connection.execute(select(func.pg_advisory_xact_lock(0x53503350)))
            locked = (await connection.execute(select(
                work_index.c.work_id, work_index.c.row_version,
            ).where(work_index.c.work_id.in_(required)).order_by(
                work_index.c.work_id,
            ).with_for_update())).all()
            if {row[0] for row in locked} != required:
                raise ValueError("parent work is not admitted")
            versions = {row[0]: row[1] for row in locked}
            if versions[work_id] != expected_row_version:
                return False
            admitted = set((await connection.execute(select(
                workset_memberships.c.work_id,
            ).where(
                workset_memberships.c.work_id.in_(required),
                workset_memberships.c.semantics == "AUTHORITATIVE",
            ))).scalars())
            if admitted != required:
                raise ValueError("parent work is not admitted")
            edges = (await connection.execute(select(
                work_parent_edges.c.child_work_id, work_parent_edges.c.parent_work_id,
            ))).all()
            self._validate_parent_graph(edges, work_id, parent_id)
            current = next((row[1] for row in edges if row[0] == work_id), None)
            if current == parent_id:
                return True
            await connection.execute(delete(work_parent_edges).where(
                work_parent_edges.c.child_work_id == work_id
            ))
            if parent_id is not None:
                await connection.execute(insert(work_parent_edges).values(
                    child_work_id=work_id, parent_work_id=parent_id, row_version=1,
                ))
            await connection.execute(update(work_index).where(
                work_index.c.work_id == work_id
            ).values(row_version=expected_row_version + 1))
        return True

    async def parent_matches(self, work_id: UUID, parent_id: UUID | None) -> bool:
        if await self.generation() is None:
            raise RuntimeError("workset authority is not active")
        async with self.engine.connect() as connection:
            current = await connection.scalar(select(
                work_parent_edges.c.parent_work_id,
            ).where(work_parent_edges.c.child_work_id == work_id))
        return current == parent_id

    async def update_workset(
        self, workset_id: UUID, expected_row_version: int, *,
        name: str | None = None, state: str | None = None,
    ) -> bool:
        """Rename or change lifecycle state under Stage 3 optimistic currentness."""
        if name is None and state is None:
            raise ValueError("workset update must change name or state")
        if name is not None and not name.strip():
            raise ValueError("workset name must contain non-whitespace text")
        if state is not None and state not in {"ACTIVE", "RETIRED"}:
            raise ValueError("invalid workset state")
        if await self.generation() is None:
            raise RuntimeError("workset authority is not active")
        async with self.engine.begin() as connection:
            await connection.execute(select(func.pg_advisory_xact_lock(0x53503350)))
            row = (await connection.execute(select(
                worksets.c.name, worksets.c.state, worksets.c.row_version,
            ).where(worksets.c.workset_id == workset_id).with_for_update())).one_or_none()
            if row is None:
                raise ValueError("workset is not admitted")
            if row[2] != expected_row_version:
                return (
                    row[2] == expected_row_version + 1
                    and (name is None or row[0] == name)
                    and (state is None or row[1] == state)
                )
            values = {
                "name": row[0] if name is None else name,
                "state": row[1] if state is None else state,
            }
            if (values["name"], values["state"]) == row[:2]:
                return True
            await connection.execute(update(worksets).where(
                worksets.c.workset_id == workset_id,
                worksets.c.row_version == expected_row_version,
            ).values(**values, row_version=expected_row_version + 1))
        return True

    async def move_authoritative_membership(
        self, work_id: UUID, from_workset_id: UUID, to_workset_id: UUID,
        expected_from_version: int, expected_to_version: int | None,
    ) -> bool:
        """Atomically demote the old home and promote the new active workset."""
        if from_workset_id == to_workset_id:
            raise ValueError("authoritative membership move requires distinct worksets")
        if await self.generation() is None:
            raise RuntimeError("workset authority is not active")
        async with self.engine.begin() as connection:
            await connection.execute(select(func.pg_advisory_xact_lock(0x53503350)))
            set_rows = (await connection.execute(select(
                worksets.c.workset_id, worksets.c.state,
            ).where(worksets.c.workset_id.in_({from_workset_id, to_workset_id})).order_by(
                worksets.c.workset_id,
            ).with_for_update())).all()
            states = {cast(UUID, row[0]): cast(str, row[1]) for row in set_rows}
            if len(set_rows) != 2 or states.get(to_workset_id) != "ACTIVE":
                raise ValueError("membership target workset is not active and admitted")
            memberships = (await connection.execute(select(
                workset_memberships.c.workset_id,
                workset_memberships.c.semantics,
                workset_memberships.c.member_role,
                workset_memberships.c.row_version,
            ).where(
                workset_memberships.c.work_id == work_id,
                workset_memberships.c.workset_id.in_({from_workset_id, to_workset_id}),
            ).order_by(workset_memberships.c.workset_id).with_for_update())).all()
            by_set = {row[0]: row for row in memberships}
            source, target = by_set.get(from_workset_id), by_set.get(to_workset_id)
            if (
                source is not None and target is not None
                and source[1:3] == ("RELATED", "MEMBER")
                and target[1:3] == ("AUTHORITATIVE", "MEMBER")
                and source[3] == expected_from_version + 1
                and target[3] == (
                    1 if expected_to_version is None else expected_to_version + 1
                )
            ):
                return True
            if source is None or source[1:3] != ("AUTHORITATIVE", "MEMBER"):
                raise ValueError("source is not a movable authoritative membership")
            if source[3] != expected_from_version:
                return False
            if target is None:
                if expected_to_version is not None:
                    return False
            elif target[1:3] != ("RELATED", "MEMBER"):
                raise ValueError("target membership is not promotable")
            elif target[3] != expected_to_version:
                return False
            await connection.execute(update(workset_memberships).where(
                workset_memberships.c.workset_id == from_workset_id,
                workset_memberships.c.work_id == work_id,
            ).values(semantics="RELATED", row_version=expected_from_version + 1))
            if target is None:
                await connection.execute(insert(workset_memberships).values(
                    workset_id=to_workset_id, work_id=work_id,
                    semantics="AUTHORITATIVE", member_role="MEMBER", row_version=1,
                ))
            else:
                assert expected_to_version is not None
                await connection.execute(update(workset_memberships).where(
                    workset_memberships.c.workset_id == to_workset_id,
                    workset_memberships.c.work_id == work_id,
                ).values(semantics="AUTHORITATIVE", row_version=expected_to_version + 1))
        return True

    async def update_related_membership(
        self, work_id: UUID, workset_id: UUID, *, add: bool,
        expected_workset_version: int,
    ) -> bool:
        """Add or remove one non-authoritative membership without moving authority."""
        if await self.generation() is None:
            raise RuntimeError("workset authority is not active")
        async with self.engine.begin() as connection:
            await connection.execute(select(func.pg_advisory_xact_lock(0x53503350)))
            workset = (await connection.execute(select(
                worksets.c.state, worksets.c.row_version,
            ).where(
                worksets.c.workset_id == workset_id,
            ).with_for_update())).one_or_none()
            if workset is None or add and workset[0] != "ACTIVE":
                raise ValueError("related membership workset is not active and admitted")
            admitted = await connection.scalar(select(work_index.c.work_id).where(
                work_index.c.work_id == work_id,
            ).with_for_update())
            if admitted is None:
                raise ValueError("related work is not admitted")
            row = (await connection.execute(select(
                workset_memberships.c.semantics,
                workset_memberships.c.member_role,
                workset_memberships.c.row_version,
            ).where(
                workset_memberships.c.workset_id == workset_id,
                workset_memberships.c.work_id == work_id,
            ).with_for_update())).one_or_none()
            desired = row is None if not add else (
                row is not None and row[:2] == ("RELATED", "MEMBER")
            )
            if workset[1] != expected_workset_version:
                return workset[1] == expected_workset_version + 1 and desired
            if add:
                if row is not None:
                    if row[:2] != ("RELATED", "MEMBER"):
                        raise ValueError("membership is not a related member")
                    return True
                await connection.execute(insert(workset_memberships).values(
                    workset_id=workset_id, work_id=work_id,
                    semantics="RELATED", member_role="MEMBER", row_version=1,
                ))
            else:
                if row is None:
                    return True
                if row[:2] != ("RELATED", "MEMBER"):
                    raise ValueError("authoritative membership cannot be removed as related")
                await connection.execute(delete(workset_memberships).where(
                    workset_memberships.c.workset_id == workset_id,
                    workset_memberships.c.work_id == work_id,
                ))
            await connection.execute(update(worksets).where(
                worksets.c.workset_id == workset_id,
                worksets.c.row_version == expected_workset_version,
            ).values(row_version=expected_workset_version + 1))
        return True

    async def transfer_master(
        self, workset_id: UUID, from_work_id: UUID, to_work_id: UUID,
        expected_from_version: int, expected_to_version: int,
    ) -> bool:
        """Atomically transfer one durable role's MASTER designation."""
        if from_work_id == to_work_id:
            raise ValueError("MASTER transfer requires distinct work")
        if await self.generation() is None:
            raise RuntimeError("workset authority is not active")
        async with self.engine.begin() as connection:
            await connection.execute(select(func.pg_advisory_xact_lock(0x53503350)))
            role = (await connection.execute(select(
                worksets.c.role_identity, worksets.c.state,
            ).where(worksets.c.workset_id == workset_id).with_for_update())).one_or_none()
            if role is None or role[0] is None or role[1] != "ACTIVE":
                raise ValueError("MASTER transfer requires an active durable role workset")
            rows = (await connection.execute(select(
                workset_memberships.c.work_id,
                workset_memberships.c.semantics,
                workset_memberships.c.member_role,
                workset_memberships.c.row_version,
            ).where(
                workset_memberships.c.workset_id == workset_id,
                workset_memberships.c.work_id.in_({from_work_id, to_work_id}),
            ).order_by(workset_memberships.c.work_id).with_for_update())).all()
            by_work = {row[0]: row for row in rows}
            source, target = by_work.get(from_work_id), by_work.get(to_work_id)
            if (
                source is not None and target is not None
                and source[1:3] == ("AUTHORITATIVE", "MEMBER")
                and target[1:3] == ("AUTHORITATIVE", "MASTER")
                and source[3] == expected_from_version + 1
                and target[3] == expected_to_version + 1
            ):
                return True
            if source is None or source[1:3] != ("AUTHORITATIVE", "MASTER"):
                raise ValueError("source is not the current MASTER")
            if target is None or target[1:3] != ("AUTHORITATIVE", "MEMBER"):
                raise ValueError("target is not an authoritative role member")
            if source[3] != expected_from_version or target[3] != expected_to_version:
                return False
            await connection.execute(update(workset_memberships).where(
                workset_memberships.c.workset_id == workset_id,
                workset_memberships.c.work_id == from_work_id,
            ).values(member_role="MEMBER", row_version=expected_from_version + 1))
            await connection.execute(update(workset_memberships).where(
                workset_memberships.c.workset_id == workset_id,
                workset_memberships.c.work_id == to_work_id,
            ).values(member_role="MASTER", row_version=expected_to_version + 1))
        return True

    async def admit_created_under_parent(
        self, work_id: UUID, parent_id: UUID, title: str, provider: ProviderWork,
    ) -> None:
        """Atomically admit one already-bound create into its parent's workset."""
        if await self.generation() is None:
            raise RuntimeError("workset authority is not active")
        values = await self.index.created_values(work_id, title, provider)
        async with self.engine.begin() as connection:
            await connection.execute(select(func.pg_advisory_xact_lock(0x53503350)))
            bound = await connection.scalar(select(work_handles.c.id).where(
                work_handles.c.id == work_id
            ).with_for_update())
            parent = (await connection.execute(select(
                workset_memberships.c.workset_id, worksets.c.state,
            ).join(worksets, worksets.c.workset_id == workset_memberships.c.workset_id).where(
                workset_memberships.c.work_id == parent_id,
                workset_memberships.c.semantics == "AUTHORITATIVE",
            ).with_for_update())).one_or_none()
            if bound is None or parent is None or parent[1] != "ACTIVE":
                raise ValueError("created work parent is not actively admitted")
            await connection.execute(pg_insert(work_index).values(values).on_conflict_do_nothing())
            indexed = (await connection.execute(select(
                work_index.c.title,
            ).where(work_index.c.work_id == work_id))).one_or_none()
            if indexed is None or indexed[0] != title:
                raise ValueError("created work admission conflict")
            await connection.execute(pg_insert(workset_memberships).values(
                workset_id=parent[0], work_id=work_id, semantics="AUTHORITATIVE",
                member_role="MEMBER", row_version=1,
            ).on_conflict_do_nothing())
            membership = (await connection.execute(select(
                workset_memberships.c.workset_id, workset_memberships.c.semantics,
                workset_memberships.c.member_role,
            ).where(workset_memberships.c.work_id == work_id))).one_or_none()
            if membership != (parent[0], "AUTHORITATIVE", "MEMBER"):
                raise ValueError("created work membership conflict")
            await connection.execute(pg_insert(work_parent_edges).values(
                child_work_id=work_id, parent_work_id=parent_id, row_version=1,
            ).on_conflict_do_nothing())
            actual_parent = await connection.scalar(select(
                work_parent_edges.c.parent_work_id,
            ).where(work_parent_edges.c.child_work_id == work_id))
            if actual_parent != parent_id:
                raise ValueError("created work parent conflict")


async def _stored_rows(
    connection: AsyncConnection, *, lock: bool = False,
) -> list[list[object]]:
    set_query = select(
        worksets.c.workset_id, worksets.c.workset_key, worksets.c.name, worksets.c.kind,
        worksets.c.role_identity, worksets.c.state, worksets.c.row_version,
    ).order_by(worksets.c.workset_id)
    membership_query = select(
        workset_memberships.c.workset_id, workset_memberships.c.work_id,
        workset_memberships.c.semantics, workset_memberships.c.member_role,
        workset_memberships.c.row_version,
    ).order_by(workset_memberships.c.workset_id, workset_memberships.c.work_id)
    parent_query = select(
        work_parent_edges.c.child_work_id, work_parent_edges.c.parent_work_id,
        work_parent_edges.c.row_version,
    ).order_by(work_parent_edges.c.child_work_id)
    if lock:
        set_query = set_query.with_for_update()
        membership_query = membership_query.with_for_update()
        parent_query = parent_query.with_for_update()
    set_rows = (await connection.execute(set_query)).all()
    membership_rows = (await connection.execute(membership_query)).all()
    parent_rows = (await connection.execute(parent_query)).all()
    result = [_row('workset', str(row[0]), *row[1:]) for row in set_rows]
    result += [_row('membership', str(row[0]), str(row[1]), *row[2:])
               for row in membership_rows]
    result += [_row('parent', str(row[0]), str(row[1]), row[2]) for row in parent_rows]
    return result


async def stage_snapshot(engine: AsyncEngine, snapshot: WorksetSnapshot) -> str:
    """Replace pre-authority staging atomically; repeating the same snapshot is a no-op."""
    snapshot.validated()
    desired = snapshot.rows()
    digest = canonical_digest(desired)
    authoritative = {
        row.work_id for row in snapshot.memberships if row.semantics == "AUTHORITATIVE"
    }
    referenced = authoritative | {
        value for row in snapshot.parent_edges for value in (row.child_work_id, row.parent_work_id)
    }
    async with engine.begin() as connection:
        await connection.execute(select(func.pg_advisory_xact_lock(0x53544733)))
        if (await connection.execute(select(workset_authority.c.scope))).first() is not None or (
            await connection.execute(select(workset_cutovers.c.scope))
        ).first() is not None:
            raise ActivationUnknown("Stage 3 authority already exists; reconcile its receipt")
        admitted = set((await connection.execute(select(work_index.c.work_id))).scalars())
        if authoritative != admitted:
            raise ValueError("authoritative memberships must exactly cover the admitted corpus")
        if not referenced <= admitted:
            raise ValueError("structure references work outside the admitted corpus")
        if canonical_digest(await _stored_rows(connection)) == digest:
            return digest
        await connection.execute(delete(work_parent_edges))
        await connection.execute(delete(workset_memberships))
        await connection.execute(delete(worksets))
        await connection.execute(insert(worksets), [row.__dict__ for row in snapshot.worksets])
        await connection.execute(
            insert(workset_memberships), [row.__dict__ for row in snapshot.memberships]
        )
        if snapshot.parent_edges:
            await connection.execute(
                insert(work_parent_edges), [row.__dict__ for row in snapshot.parent_edges]
            )
        if canonical_digest(await _stored_rows(connection)) != digest:
            raise RuntimeError("Stage 3 snapshot readback mismatch")
    return digest


async def reconcile_staging(engine: AsyncEngine, expected_digest: str) -> str:
    """Prove exact pre-authority staged rows without changing them."""
    async with engine.connect() as connection:
        if (await connection.execute(select(workset_authority.c.scope))).first() is not None or (
            await connection.execute(select(workset_cutovers.c.scope))
        ).first() is not None:
            raise ActivationUnknown("Stage 3 authority already exists; reconcile its receipt")
        rows = await _stored_rows(connection)
    if not rows or canonical_digest(rows) != expected_digest:
        raise ValueError("Stage 3 staged snapshot does not match the reviewed worksheet")
    return expected_digest


async def reset_staging(engine: AsyncEngine, expected_digest: str) -> None:
    """Delete only exact reviewed staging before the irreversible marker exists."""
    async with engine.begin() as connection:
        await connection.execute(select(func.pg_advisory_xact_lock(0x53544733)))
        if (await connection.execute(select(workset_authority.c.scope))).first() is not None or (
            await connection.execute(select(workset_cutovers.c.scope))
        ).first() is not None:
            raise ActivationUnknown("Stage 3 authority already exists; reset is forbidden")
        rows = await _stored_rows(connection)
        if not rows or canonical_digest(rows) != expected_digest:
            raise ValueError("Stage 3 staged snapshot does not match the reviewed worksheet")
        await connection.execute(delete(work_parent_edges))
        await connection.execute(delete(workset_memberships))
        await connection.execute(delete(worksets))


async def _authority_pair(
    connection: AsyncConnection, authority: Table, cutovers: Table, *, lock: bool = True,
) -> tuple[tuple[str, int] | None, tuple[int] | None]:
    authority_query = select(
        authority.c.state, authority.c.generation,
    ).where(authority.c.scope == SCOPE)
    cutover_query = select(
        cutovers.c.generation,
    ).where(cutovers.c.scope == SCOPE)
    if lock:
        authority_query = authority_query.with_for_update()
        cutover_query = cutover_query.with_for_update()
    authority_row = cast(
        tuple[str, int] | None,
        (await connection.execute(authority_query)).one_or_none(),
    )
    cutover_row = cast(
        tuple[int] | None,
        (await connection.execute(cutover_query)).one_or_none(),
    )
    return authority_row, cutover_row


def _paired_authority(
    pair: tuple[tuple[str, int] | None, tuple[int] | None],
) -> bool:
    authority, cutover = pair
    return (
        authority is not None and cutover is not None
        and authority[0] == AUTHORITY and authority[1] == cutover[0]
    )


async def _activation_state(
    connection: AsyncConnection, *, lock: bool = False,
) -> tuple[object, object, str]:
    authority_query = select(
        workset_authority.c.state, workset_authority.c.generation,
    ).where(workset_authority.c.scope == SCOPE)
    cutover_query = select(workset_cutovers.c.generation).where(
        workset_cutovers.c.scope == SCOPE
    )
    if lock:
        authority_query = authority_query.with_for_update()
        cutover_query = cutover_query.with_for_update()
    authority = (await connection.execute(authority_query)).one_or_none()
    cutover = (await connection.execute(cutover_query)).one_or_none()
    return authority, cutover, canonical_digest(await _stored_rows(connection, lock=lock))


async def _commit(transaction: AsyncTransaction) -> None:
    await transaction.commit()


async def reconcile_activation(
    engine: AsyncEngine, receipt: ActivationReceipt,
) -> ActivationReceipt:
    """Classify one exact Stage 3 marker attempt without repeating the flip."""
    try:
        async with engine.connect() as connection:
            authority, cutover, digest = await _activation_state(connection)
            prior_valid = all((
                _paired_authority(await _authority_pair(
                    connection, work_authority, work_authority_cutovers, lock=False,
                )),
                _paired_authority(await _authority_pair(
                    connection, work_metadata_authority, work_metadata_cutovers, lock=False,
                )),
            ))
    except BaseException as error:
        raise ActivationUnknown(
            "Stage 3 activation outcome UNKNOWN; keep the maintenance gate"
        ) from error
    if (
        authority == (AUTHORITY, receipt.generation)
        and cutover == (receipt.generation,)
        and digest == receipt.corpus_digest
        and prior_valid
    ):
        return replace(receipt, recovered_after_commit_error=True)
    if authority is None and cutover is None and digest == receipt.pre_corpus_digest:
        raise ActivationNotCommitted("Stage 3 readback proves authority was not activated")
    raise ActivationUnknown("Stage 3 activation outcome UNKNOWN; repair forward")


async def activate(
    engine: AsyncEngine, expected_digest: str,
    before_commit: Callable[[ActivationReceipt], None] | None = None,
) -> ActivationReceipt:
    """Validate exact offline staging and atomically flip Stage 3 authority."""
    connection = await engine.connect()
    transaction = await connection.begin()
    commit_started = False
    receipt = ActivationReceipt(0, 1, expected_digest, pre_corpus_digest=expected_digest)
    try:
        await connection.execute(select(func.pg_advisory_xact_lock(0x53544733)))
        stage1 = await _authority_pair(connection, work_authority, work_authority_cutovers)
        stage2 = await _authority_pair(
            connection, work_metadata_authority, work_metadata_cutovers,
        )
        for label, pair in (("Stage 1", stage1), ("Stage 2", stage2)):
            if not _paired_authority(pair):
                raise ValueError(f"{label} paired authority is required")
        authority, cutover, digest = await _activation_state(connection, lock=True)
        if authority is not None or cutover is not None:
            raise ActivationUnknown("Stage 3 authority already exists; reconcile its receipt")
        admitted = set((await connection.execute(select(
            work_index.c.work_id,
        ).with_for_update())).scalars())
        authoritative = set((await connection.execute(select(
            workset_memberships.c.work_id,
        ).where(
            workset_memberships.c.semantics == "AUTHORITATIVE",
        ))).scalars())
        if not admitted or authoritative != admitted:
            raise ValueError("authoritative memberships must exactly cover the admitted corpus")
        if digest != expected_digest:
            raise ValueError("Stage 3 staged snapshot does not match the reviewed worksheet")
        await connection.execute(insert(workset_authority).values(
            scope=SCOPE, state=AUTHORITY, generation=1,
        ))
        await connection.execute(insert(workset_cutovers).values(
            scope=SCOPE, generation=1,
        ))
        receipt = ActivationReceipt(
            len(authoritative), 1, digest, pre_corpus_digest=digest,
        )
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
    try:
        async with engine.connect() as readback:
            authority, cutover, digest = await _activation_state(readback)
    except BaseException as error:
        raise ActivationUnknown(
            "Stage 3 committed readback UNKNOWN; reconcile the durable receipt"
        ) from error
    if authority != (AUTHORITY, 1) or cutover != (1,) or digest != expected_digest:
        raise ActivationUnknown("Stage 3 committed readback is inconsistent; repair forward")
    return receipt
