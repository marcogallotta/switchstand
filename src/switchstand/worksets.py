"""Inert Stage 3 workset snapshot validation and staging."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .contracts import Routing, WorkContext, WorkSearchItem
from .discovery import DiscoveredStructure
from .state import (
    work_edges,
    work_index,
    work_parent_edges,
    workset_authority,
    workset_cutovers,
    workset_memberships,
    worksets,
)
from .work_index import (
    INDEX_COLUMNS,
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
    workset: Workset
    members: tuple[WorksetMember, ...]


class WorksetReader:
    """Default-off Stage 3 reads; inconsistent irreversible state fails closed."""

    def __init__(self, engine: AsyncEngine):
        self.engine, self.index = engine, WorkIndex(engine)

    async def generation(self) -> int | None:
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(
                select(workset_authority.c.state).where(
                    workset_authority.c.scope == SCOPE).scalar_subquery(),
                select(workset_authority.c.generation).where(
                    workset_authority.c.scope == SCOPE).scalar_subquery(),
                select(workset_cutovers.c.generation).where(
                    workset_cutovers.c.scope == SCOPE).scalar_subquery(),
            ))).one()
        if row == (None, None, None):
            return None
        if (row[0] != AUTHORITY or row[1] != row[2]
                or not isinstance(row[1], int) or row[1] < 1):
            raise ValueError("inconsistent irreversible workset authority state")
        from .work_metadata import authority_generation
        if await self.index.generation() is None or await authority_generation(self.engine) is None:
            raise ValueError("Stage 3 authority requires Stage 1 and Stage 2 authority")
        return row[1]

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

    async def enumerate(self, workset_id: UUID) -> WorksetView | None:
        """Return every nonterminal member and its Stage 2 dependency truth."""
        if await self.generation() is None:
            return None
        index_generation = await self.index.generation()
        if index_generation is None:
            raise ValueError("Stage 3 authority requires Stage 1 authority")
        async with self.engine.connect() as connection:
            set_row = (await connection.execute(select(
                worksets.c.workset_id, worksets.c.workset_key, worksets.c.name,
                worksets.c.kind, worksets.c.role_identity, worksets.c.state,
                worksets.c.row_version,
            ).where(worksets.c.workset_id == workset_id))).one_or_none()
            if set_row is None:
                raise PermissionError("workset is not admitted")
            rows = (await connection.execute(select(
                *INDEX_COLUMNS, workset_memberships.c.semantics,
                workset_memberships.c.member_role,
            ).join(workset_memberships,
                   workset_memberships.c.work_id == work_index.c.work_id).where(
                workset_memberships.c.workset_id == workset_id,
                work_index.c.completed.is_(False),
            ).order_by(work_index.c.normalized_title, work_index.c.work_id))).all()
            member_ids = tuple(row[0] for row in rows)
            edges = (await connection.execute(select(
                work_edges.c.work_id, work_edges.c.depends_on_work_id,
            ).where(work_edges.c.work_id.in_(member_ids)).order_by(
                work_edges.c.work_id, work_edges.c.depends_on_work_id,
            ))).all() if member_ids else ()
        dependencies: dict[UUID, list[UUID]] = {}
        for owner, dependency in edges:
            dependencies.setdefault(owner, []).append(dependency)
        return WorksetView(self._workset(set_row), tuple(
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
        return await self.enumerate(workset_id)

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


async def _stored_rows(connection: AsyncConnection) -> list[list[object]]:
    set_rows = (await connection.execute(select(
        worksets.c.workset_id, worksets.c.workset_key, worksets.c.name, worksets.c.kind,
        worksets.c.role_identity, worksets.c.state, worksets.c.row_version,
    ).order_by(worksets.c.workset_id))).all()
    membership_rows = (await connection.execute(select(
        workset_memberships.c.workset_id, workset_memberships.c.work_id,
        workset_memberships.c.semantics, workset_memberships.c.member_role,
        workset_memberships.c.row_version,
    ).order_by(workset_memberships.c.workset_id, workset_memberships.c.work_id))).all()
    parent_rows = (await connection.execute(select(
        work_parent_edges.c.child_work_id, work_parent_edges.c.parent_work_id,
        work_parent_edges.c.row_version,
    ).order_by(work_parent_edges.c.child_work_id))).all()
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
