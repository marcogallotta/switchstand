"""Inert Stage 3 workset snapshot validation and staging."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .state import (
    work_index,
    work_parent_edges,
    workset_authority,
    workset_cutovers,
    workset_memberships,
    worksets,
)
from .work_index import ActivationUnknown, canonical_digest


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
