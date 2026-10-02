import os
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.canonical_relations import (
    CanonicalRelationsRepository,
    ProjectPlacement,
    project_memberships,
    projects,
    work_dependencies,
    work_parents,
)
from switchstand.canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_metadata,
    canonical_work,
)
from switchstand.state import metadata as shared_metadata

RELATION_TABLES = [work_dependencies, work_parents, projects, project_memberships]
REQUIRED_TABLES = [canonical_work, *RELATION_TABLES]


@pytest.fixture
async def repositories(
    database_prerequisite: None,
) -> AsyncGenerator[
    tuple[CanonicalWorkRepository, CanonicalRelationsRepository, set[UUID]]
]:
    del database_prerequisite
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    owned_work_ids: set[UUID] = set()
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: canonical_metadata.create_all(
            sync, tables=REQUIRED_TABLES, checkfirst=True,
        ))
    yield CanonicalWorkRepository(engine), CanonicalRelationsRepository(engine), owned_work_ids
    async with engine.begin() as connection:
        project_ids = tuple((await connection.scalars(
            project_memberships.select().with_only_columns(
                project_memberships.c.project_id
            ).where(project_memberships.c.work_id.in_(owned_work_ids))
        )).all())
        await connection.execute(project_memberships.delete().where(
            project_memberships.c.work_id.in_(owned_work_ids)
        ))
        await connection.execute(work_dependencies.delete().where(
            work_dependencies.c.work_id.in_(owned_work_ids)
            | work_dependencies.c.depends_on_work_id.in_(owned_work_ids)
        ))
        await connection.execute(work_parents.delete().where(
            work_parents.c.child_work_id.in_(owned_work_ids)
            | work_parents.c.parent_work_id.in_(owned_work_ids)
        ))
        await connection.execute(projects.delete().where(projects.c.project_id.in_(project_ids)))
        await connection.execute(canonical_work.delete().where(
            canonical_work.c.work_id.in_(owned_work_ids)
        ))
        await connection.run_sync(lambda sync: canonical_metadata.drop_all(
            sync, tables=list(reversed(RELATION_TABLES)), checkfirst=True,
        ))
    await engine.dispose()


def test_compact_relation_schema_is_isolated_and_has_no_workset_domain() -> None:
    names = {
        "work_dependencies", "work_parents", "projects", "project_memberships",
    }
    assert names.isdisjoint(shared_metadata.tables)
    assert work_dependencies.primary_key.columns.keys() == [
        "work_id", "depends_on_work_id",
    ]
    assert work_parents.primary_key.columns.keys() == ["child_work_id"]
    assert projects.primary_key.columns.keys() == ["project_id"]
    assert project_memberships.primary_key.columns.keys() == ["project_id", "work_id"]
    assert set(canonical_metadata.tables) >= names | {"canonical_work"}
    assert not any(
        word in column.name
        for table in (work_dependencies, work_parents, projects, project_memberships)
        for column in table.columns
        for word in ("workset", "role", "master", "horizon", "authority")
    )


async def _create(
    works: CanonicalWorkRepository, owned_work_ids: set[UUID], work_id: UUID, title: str,
) -> None:
    await works.create(CurrentWork(work_id, title, False, f"notes for {title}"))
    owned_work_ids.add(work_id)


async def test_real_postgres_relations_placements_versions_and_rollbacks(
    repositories: tuple[CanonicalWorkRepository, CanonicalRelationsRepository, set[UUID]],
) -> None:
    works, relations, owned_work_ids = repositories
    child, parent, ancestor, dependency = uuid4(), uuid4(), uuid4(), uuid4()
    for work_id, title in (
        (child, "Child"), (parent, "Parent"),
        (ancestor, "Ancestor"), (dependency, "Dependency"),
    ):
        await _create(works, owned_work_ids, work_id, title)

    empty = await relations.get(child)
    assert empty.parent_work_id is None
    assert empty.dependency_work_ids == ()
    assert empty.placements == ()

    assert await relations.set_parent(child, parent, 1) == 2
    assert await relations.set_parent(parent, ancestor, 1) == 2
    assert (await relations.get(child)).parent_work_id == parent
    assert await relations.set_parent(child, parent, 2) == 2

    with pytest.raises(ValueError, match="cycle"):
        await relations.set_parent(ancestor, child, 1)
    assert (await works.get(ancestor)).row_version == 1  # type: ignore[union-attr]
    assert (await relations.get(ancestor)).parent_work_id is None

    with pytest.raises(ValueError, match="stale"):
        await relations.set_parent(child, ancestor, 1)
    assert (await relations.get(child)).parent_work_id == parent

    assert await relations.change_dependency(
        child, dependency, add=True, observed_version=2,
    ) == 3
    assert await relations.change_dependency(
        child, dependency, add=True, observed_version=3,
    ) == 3
    assert (await relations.get(child)).dependency_work_ids == (dependency,)

    first_project, second_project = uuid4(), uuid4()
    placements = (
        ProjectPlacement(first_project, "Area", "111", "Current"),
        ProjectPlacement(second_project, "Review", "222"),
    )
    assert await relations.replace_placements(child, placements, 3) == 4
    stored = await relations.get(child)
    assert stored.placements == placements
    assert (await works.get(child)).row_version == 4  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="project identity"):
        await relations.replace_placements(child, (
            ProjectPlacement(first_project, "Renamed", "111"),
        ), 4)
    assert (await works.get(child)).row_version == 4  # type: ignore[union-attr]
    assert (await relations.get(child)).placements == placements

    with pytest.raises(ValueError, match="stale"):
        await relations.replace_placements(child, (), 3)
    assert (await relations.get(child)).placements == placements

    assert await relations.change_dependency(
        child, dependency, add=False, observed_version=4,
    ) == 5
    assert (await relations.get(child)).dependency_work_ids == ()


async def test_real_postgres_rejects_unknown_and_invalid_targets_without_changes(
    repositories: tuple[CanonicalWorkRepository, CanonicalRelationsRepository, set[UUID]],
) -> None:
    works, relations, owned_work_ids = repositories
    work_id, missing = uuid4(), uuid4()
    await _create(works, owned_work_ids, work_id, "Known")

    with pytest.raises(LookupError, match="parent"):
        await relations.set_parent(work_id, missing, 1)
    with pytest.raises(LookupError, match="dependency"):
        await relations.change_dependency(
            work_id, missing, add=True, observed_version=1,
        )
    with pytest.raises(ValueError, match="itself"):
        await relations.set_parent(work_id, work_id, 1)
    with pytest.raises(ValueError, match="itself"):
        await relations.change_dependency(
            work_id, work_id, add=True, observed_version=1,
        )
    with pytest.raises(ValueError, match="unique"):
        await relations.replace_placements(work_id, (
            ProjectPlacement(missing, "One"), ProjectPlacement(missing, "Two"),
        ), 1)

    assert (await works.get(work_id)).row_version == 1  # type: ignore[union-attr]
    assert await relations.get(work_id) == type(await relations.get(work_id))(None, (), ())
