"""Inert compact relations and project placement persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Table,
    Text,
    delete,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .canonical_work import canonical_metadata, canonical_work

work_dependencies = Table(
    "work_dependencies", canonical_metadata,
    Column("work_id", PGUUID(as_uuid=True),
           ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), primary_key=True),
    Column("depends_on_work_id", PGUUID(as_uuid=True),
           ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), primary_key=True),
    CheckConstraint("work_id <> depends_on_work_id", name="ck_work_dependency_not_self"),
)
work_parents = Table(
    "work_parents", canonical_metadata,
    Column("child_work_id", PGUUID(as_uuid=True),
           ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), primary_key=True),
    Column("parent_work_id", PGUUID(as_uuid=True),
           ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), nullable=False, index=True),
    CheckConstraint("child_work_id <> parent_work_id", name="ck_work_parent_not_self"),
)
projects = Table(
    "projects", canonical_metadata,
    Column("project_id", PGUUID(as_uuid=True), primary_key=True),
    Column("asana_project_gid", Text, unique=True),
    Column("name", Text, nullable=False),
    CheckConstraint("asana_project_gid IS NULL OR asana_project_gid <> ''",
                    name="ck_project_legacy_gid"),
    CheckConstraint("name <> ''", name="ck_project_name"),
)
project_memberships = Table(
    "project_memberships", canonical_metadata,
    Column("project_id", PGUUID(as_uuid=True),
           ForeignKey("projects.project_id", ondelete="RESTRICT"), primary_key=True),
    Column("work_id", PGUUID(as_uuid=True),
           ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), primary_key=True),
    Column("section_name", Text),
    CheckConstraint("section_name IS NULL OR section_name <> ''", name="ck_project_section"),
)


@dataclass(frozen=True, order=True)
class ProjectPlacement:
    project_id: UUID
    name: str
    asana_project_gid: str | None = None
    section_name: str | None = None


@dataclass(frozen=True)
class WorkRelations:
    parent_work_id: UUID | None
    dependency_work_ids: tuple[UUID, ...]
    placements: tuple[ProjectPlacement, ...]


async def _locked_version(
    connection: AsyncConnection, work_id: UUID, observed_version: int,
) -> int:
    version = await connection.scalar(select(canonical_work.c.row_version).where(
        canonical_work.c.work_id == work_id
    ).with_for_update())
    if version is None:
        raise LookupError("canonical work does not exist")
    if version != observed_version:
        raise ValueError("stale canonical work version")
    return cast(int, version)


async def _bump(connection: AsyncConnection, work_id: UUID, version: int) -> int:
    next_version = version + 1
    await connection.execute(update(canonical_work).where(
        canonical_work.c.work_id == work_id
    ).values(row_version=next_version))
    return next_version


class CanonicalRelationsRepository:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def get(self, work_id: UUID) -> WorkRelations:
        async with self.engine.connect() as connection:
            if await connection.scalar(select(canonical_work.c.work_id).where(
                canonical_work.c.work_id == work_id
            )) is None:
                raise LookupError("canonical work does not exist")
            parent = await connection.scalar(select(work_parents.c.parent_work_id).where(
                work_parents.c.child_work_id == work_id
            ))
            dependencies = tuple((await connection.scalars(select(
                work_dependencies.c.depends_on_work_id
            ).where(work_dependencies.c.work_id == work_id).order_by(
                work_dependencies.c.depends_on_work_id
            ))).all())
            rows = (await connection.execute(select(
                projects.c.project_id, projects.c.name, projects.c.asana_project_gid,
                project_memberships.c.section_name,
            ).join(project_memberships).where(
                project_memberships.c.work_id == work_id
            ).order_by(projects.c.name, projects.c.project_id))).all()
        return WorkRelations(
            cast(UUID | None, parent), dependencies,
            tuple(ProjectPlacement(*row) for row in rows),
        )

    async def set_parent(
        self, work_id: UUID, parent_work_id: UUID | None, observed_version: int, *,
        connection: AsyncConnection | None = None,
    ) -> int:
        if parent_work_id == work_id:
            raise ValueError("work cannot parent itself")
        if connection is None:
            async with self.engine.begin() as owned:
                return await self.set_parent(
                    work_id, parent_work_id, observed_version, connection=owned
                )
        else:
            version = await _locked_version(connection, work_id, observed_version)
            await connection.execute(text(
                "LOCK TABLE work_parents IN SHARE ROW EXCLUSIVE MODE"
            ))
            current = await connection.scalar(select(work_parents.c.parent_work_id).where(
                work_parents.c.child_work_id == work_id
            ))
            if current == parent_work_id:
                return version
            if parent_work_id is not None:
                if await connection.scalar(select(canonical_work.c.work_id).where(
                    canonical_work.c.work_id == parent_work_id
                )) is None:
                    raise LookupError("parent work does not exist")
                cycle = await connection.scalar(text(
                    "WITH RECURSIVE ancestors(work_id) AS ("
                    " SELECT CAST(:parent AS uuid) UNION ALL"
                    " SELECT p.parent_work_id FROM work_parents p"
                    " JOIN ancestors a ON p.child_work_id = a.work_id)"
                    " SELECT true FROM ancestors WHERE work_id = CAST(:child AS uuid) LIMIT 1"
                ), {"parent": str(parent_work_id), "child": str(work_id)})
                if cycle:
                    raise ValueError("parent relation would create a cycle")
            await connection.execute(delete(work_parents).where(
                work_parents.c.child_work_id == work_id
            ))
            if parent_work_id is not None:
                await connection.execute(insert(work_parents).values(
                    child_work_id=work_id, parent_work_id=parent_work_id
                ))
            return await _bump(connection, work_id, version)

    async def change_dependency(
        self, work_id: UUID, depends_on_work_id: UUID, *, add: bool,
        observed_version: int, connection: AsyncConnection | None = None,
    ) -> int:
        if depends_on_work_id == work_id:
            raise ValueError("work cannot depend on itself")
        if connection is None:
            async with self.engine.begin() as owned:
                return await self.change_dependency(
                    work_id, depends_on_work_id, add=add,
                    observed_version=observed_version, connection=owned,
                )
        else:
            version = await _locked_version(connection, work_id, observed_version)
            if await connection.scalar(select(canonical_work.c.work_id).where(
                canonical_work.c.work_id == depends_on_work_id
            )) is None:
                raise LookupError("dependency work does not exist")
            condition = (
                (work_dependencies.c.work_id == work_id)
                & (work_dependencies.c.depends_on_work_id == depends_on_work_id)
            )
            exists = await connection.scalar(select(
                work_dependencies.c.work_id
            ).where(condition))
            if bool(exists) == add:
                return version
            if add:
                await connection.execute(insert(work_dependencies).values(
                    work_id=work_id, depends_on_work_id=depends_on_work_id
                ))
            else:
                await connection.execute(delete(work_dependencies).where(condition))
            return await _bump(connection, work_id, version)

    async def change_project_membership(
        self, work_id: UUID, project_id: UUID, *, add: bool,
        observed_version: int, connection: AsyncConnection | None = None,
    ) -> int:
        """Add or remove one admitted project membership without disturbing others."""
        if connection is None:
            async with self.engine.begin() as owned:
                return await self.change_project_membership(
                    work_id, project_id, add=add,
                    observed_version=observed_version, connection=owned,
                )
        version = await _locked_version(connection, work_id, observed_version)
        if await connection.scalar(select(projects.c.project_id).where(
            projects.c.project_id == project_id
        )) is None:
            raise LookupError("project does not exist")
        condition = (
            (project_memberships.c.work_id == work_id)
            & (project_memberships.c.project_id == project_id)
        )
        exists = await connection.scalar(select(project_memberships.c.work_id).where(condition))
        if bool(exists) == add:
            return version
        if add:
            await connection.execute(insert(project_memberships).values(
                work_id=work_id, project_id=project_id, section_name=None,
            ))
        else:
            await connection.execute(delete(project_memberships).where(condition))
        confirmed = await connection.scalar(select(project_memberships.c.work_id).where(condition))
        if bool(confirmed) != add:
            raise SQLAlchemyError("project membership readback did not converge")
        return await _bump(connection, work_id, version)

    async def replace_placements(
        self, work_id: UUID, values: tuple[ProjectPlacement, ...], observed_version: int,
    ) -> int:
        if len({value.project_id for value in values}) != len(values):
            raise ValueError("project placements must be unique")
        if any(not value.name or value.asana_project_gid == "" or value.section_name == ""
               for value in values):
            raise ValueError("project placement text must be non-empty")
        async with self.engine.begin() as connection:
            version = await _locked_version(connection, work_id, observed_version)
            for value in values:
                await connection.execute(pg_insert(projects).values(
                    project_id=value.project_id, asana_project_gid=value.asana_project_gid,
                    name=value.name,
                ).on_conflict_do_nothing(index_elements=[projects.c.project_id]))
                stored = (await connection.execute(select(
                    projects.c.name, projects.c.asana_project_gid,
                ).where(projects.c.project_id == value.project_id))).one()
                if stored != (value.name, value.asana_project_gid):
                    raise ValueError("project identity conflicts with stored project")
            await connection.execute(delete(project_memberships).where(
                project_memberships.c.work_id == work_id
            ))
            if values:
                await connection.execute(insert(project_memberships), [{
                    "project_id": value.project_id, "work_id": work_id,
                    "section_name": value.section_name,
                } for value in values])
            return await _bump(connection, work_id, version)
