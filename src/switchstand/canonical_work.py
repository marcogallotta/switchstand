"""Compact PostgreSQL owner for canonical current work.

This repository is inert until the public work tools are explicitly rewired to it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .state import (
    canonical_dependencies,
    canonical_parents,
    canonical_project_memberships,
    canonical_projects,
    canonical_work,
    legacy_work_aliases,
)
from .work_index import normalize_title


@dataclass(frozen=True)
class ProjectPlacement:
    project_id: UUID
    name: str
    section_name: str | None = None
    asana_project_gid: str | None = None


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
    parent_id: UUID | None = None
    dependencies: tuple[UUID, ...] = ()
    projects: tuple[ProjectPlacement, ...] = ()


_SCALARS = (
    "title", "completed", "notes", "priority", "work_type", "lifecycle_state",
    "review_next_action", "wait_kind", "unblock_condition", "next_due",
)
_COLUMNS = (canonical_work.c.work_id, *[canonical_work.c[name] for name in _SCALARS],
            canonical_work.c.row_version)


async def _lock_and_bump_work_row(
    connection: AsyncConnection, work_id: UUID, observed_version: int | None = None,
) -> int:
    """Lock one canonical row and advance its version inside the caller's transaction."""
    current = await connection.scalar(select(canonical_work.c.row_version).where(
        canonical_work.c.work_id == work_id
    ).with_for_update())
    if current is None:
        raise LookupError("canonical work does not exist")
    if observed_version is not None and current != observed_version:
        raise ValueError("stale canonical work version")
    await connection.execute(update(canonical_work).where(
        canonical_work.c.work_id == work_id
    ).values(row_version=current + 1))
    return current + 1


class CanonicalWorkRepository:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def get(self, work_id: UUID) -> CurrentWork | None:
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(*_COLUMNS).where(
                canonical_work.c.work_id == work_id
            ))).one_or_none()
            if row is None:
                return None
            parent = await connection.scalar(select(canonical_parents.c.parent_work_id).where(
                canonical_parents.c.child_work_id == work_id
            ))
            dependencies = tuple((await connection.scalars(select(
                canonical_dependencies.c.depends_on_work_id
            ).where(canonical_dependencies.c.work_id == work_id).order_by(
                canonical_dependencies.c.depends_on_work_id
            ))).all())
            project_rows = (await connection.execute(select(
                canonical_projects.c.project_id, canonical_projects.c.name,
                canonical_project_memberships.c.section_name,
                canonical_projects.c.asana_project_gid,
            ).join(canonical_project_memberships).where(
                canonical_project_memberships.c.work_id == work_id
            ).order_by(canonical_projects.c.project_id))).all()
        values = dict(zip(_SCALARS, row[1:-1], strict=True))
        return CurrentWork(
            work_id=row[0], row_version=row[-1], parent_id=parent,
            dependencies=dependencies,
            projects=tuple(ProjectPlacement(*project) for project in project_rows), **values,
        )

    async def search_ids(
        self, query: str, *, completed: bool | None = None, limit: int = 100,
    ) -> tuple[UUID, ...]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        normalized = normalize_title(query)
        statement = select(canonical_work.c.work_id).where(
            canonical_work.c.normalized_title.contains(normalized)
        )
        if completed is not None:
            statement = statement.where(canonical_work.c.completed == completed)
        statement = statement.order_by(
            canonical_work.c.normalized_title, canonical_work.c.work_id
        ).limit(limit)
        async with self.engine.connect() as connection:
            return tuple((await connection.scalars(statement)).all())

    async def resolve_asana_gid(self, gid: str) -> UUID | None:
        async with self.engine.connect() as connection:
            return await connection.scalar(select(legacy_work_aliases.c.work_id).where(
                legacy_work_aliases.c.asana_task_gid == gid
            ))

    async def bind_legacy_gid(self, gid: str, work_id: UUID) -> None:
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
            await self._replace_relations(connection, item)

    async def replace(self, item: CurrentWork) -> CurrentWork:
        async with self.engine.begin() as connection:
            new_version = await _lock_and_bump_work_row(
                connection, item.work_id, item.row_version
            )
            await connection.execute(update(canonical_work).where(
                canonical_work.c.work_id == item.work_id
            ).values(
                normalized_title=normalize_title(item.title),
                **{name: getattr(item, name) for name in _SCALARS},
            ))
            await self._replace_relations(connection, item)
        return replace(item, row_version=new_version)

    @staticmethod
    async def _replace_relations(connection: AsyncConnection, item: CurrentWork) -> None:
        if item.work_id in item.dependencies or item.parent_id == item.work_id:
            raise ValueError("work cannot relate to itself")
        if len(set(item.dependencies)) != len(item.dependencies):
            raise ValueError("dependencies must be unique")
        await connection.execute(delete(canonical_dependencies).where(
            canonical_dependencies.c.work_id == item.work_id
        ))
        if item.dependencies:
            await connection.execute(insert(canonical_dependencies), [{
                "work_id": item.work_id, "depends_on_work_id": dependency,
            } for dependency in item.dependencies])
        await connection.execute(delete(canonical_parents).where(
            canonical_parents.c.child_work_id == item.work_id
        ))
        if item.parent_id is not None:
            await connection.execute(insert(canonical_parents).values(
                child_work_id=item.work_id, parent_work_id=item.parent_id
            ))
        await connection.execute(delete(canonical_project_memberships).where(
            canonical_project_memberships.c.work_id == item.work_id
        ))
        for project in item.projects:
            await connection.execute(pg_insert(canonical_projects).values(
                project_id=project.project_id, name=project.name,
                asana_project_gid=project.asana_project_gid,
            ).on_conflict_do_update(index_elements=[canonical_projects.c.project_id], set_={
                "name": project.name, "asana_project_gid": project.asana_project_gid,
            }))
            await connection.execute(insert(canonical_project_memberships).values(
                project_id=project.project_id, work_id=item.work_id,
                section_name=project.section_name,
            ))
