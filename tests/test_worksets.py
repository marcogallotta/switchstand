import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.state import work_handles, work_index, workset_authority
from switchstand.work_index import ActivationUnknown
from switchstand.worksets import Membership, ParentEdge, Workset, WorksetSnapshot, stage_snapshot


def database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    return url


@pytest.fixture(autouse=True)
def clean_worksets(database_prerequisite):
    engine = create_engine(database_url())
    statement = text("TRUNCATE work_parent_edges, workset_memberships, worksets, "
                     "workset_cutovers, workset_authority, work_index, work_handles CASCADE")
    try:
        with engine.begin() as connection:
            connection.execute(statement)
        yield
    finally:
        with engine.begin() as connection:
            connection.execute(statement)
        engine.dispose()


async def admit(engine, *work_ids):
    async with engine.begin() as connection:
        for position, work_id in enumerate(work_ids):
            await connection.execute(
                insert(work_handles).values(id=work_id, provider="asana",
                                            provider_work_id=f"task-{position}")
            )
            await connection.execute(
                insert(work_index).values(
                    work_id=work_id, title=f"Task {position}",
                    normalized_title=f"task {position}", completed=False,
                    provider_revision="1", row_version=1, routing={}, context={},
                )
            )


@pytest.mark.asyncio
async def test_stage_snapshot_is_complete_atomic_and_idempotent(database_prerequisite):
    engine = create_async_engine(database_url())
    master, child = uuid4(), uuid4()
    role_set, project_set = uuid4(), uuid4()
    await admit(engine, master, child)
    snapshot = WorksetSnapshot(
        worksets=(
            Workset(role_set, "agent.coordinator", "Coordinator", "DURABLE_ROLE", "Coordinator"),
            Workset(project_set, "project.tests", "Tests & CI", "PROJECT"),
        ),
        memberships=(
            Membership(role_set, master, "AUTHORITATIVE", "MASTER"),
            Membership(project_set, child, "AUTHORITATIVE"),
            Membership(project_set, master, "RELATED"),
        ),
        parent_edges=(ParentEdge(child, master),),
    )
    cyclic = WorksetSnapshot(snapshot.worksets, snapshot.memberships,
                             (ParentEdge(master, child), ParentEdge(child, master)))
    with pytest.raises(ValueError, match="cycle"):
        cyclic.validated()
    ambiguous = WorksetSnapshot(snapshot.worksets, snapshot.memberships + (
        Membership(role_set, child, "AUTHORITATIVE"),), snapshot.parent_edges)
    with pytest.raises(IntegrityError):
        await stage_snapshot(engine, ambiguous)
    digest = await stage_snapshot(engine, snapshot)
    async with engine.connect() as connection:
        query = text("SELECT workset_key, xmin::text FROM worksets ORDER BY workset_key")
        before = (await connection.execute(query)).all()
    assert await stage_snapshot(engine, snapshot) == digest
    async with engine.connect() as connection:
        after = (await connection.execute(query)).all()
    assert after == before

    async with engine.begin() as connection:
        await connection.execute(insert(workset_authority).values(
            scope="workspace", state="POSTGRES_AUTHORITY", generation=1,
        ))
    with pytest.raises(ActivationUnknown, match="authority already exists"):
        await stage_snapshot(engine, snapshot)
    await engine.dispose()


def test_downgrade_refuses_after_stage3_authority(database_prerequisite):
    engine = create_engine(database_url())
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url())
    with engine.begin() as connection:
        connection.execute(workset_authority.insert().values(
            scope="workspace", state="POSTGRES_AUTHORITY", generation=1,
        ))
    with pytest.raises(RuntimeError, match="Stage 3 authority truth"):
        command.downgrade(config, "0010_work_metadata_authority")
    with engine.begin() as connection:
        connection.execute(workset_authority.delete())
    command.downgrade(config, "0010_work_metadata_authority")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(select(text("version_num")).select_from(
            text("alembic_version"))) == "0011_workset_authority"
    engine.dispose()
