import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from chatgpt_fixture import Provider
from sqlalchemy import create_engine, delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.contracts import (
    LaunchAuthority,
    SourceTaskRequest,
    WorkAttachmentsRequest,
    WorkGetRequest,
)
from switchstand.core import Controller
from switchstand.state import (
    PostgresState,
    work_authority,
    work_authority_cutovers,
    work_edges,
    work_handles,
    work_index,
    work_metadata_authority,
    work_metadata_cutovers,
    workset_authority,
    workset_cutovers,
    workset_memberships,
)
from switchstand.work_index import ActivationUnknown
from switchstand.worksets import (
    Membership,
    ParentEdge,
    Workset,
    WorksetReader,
    WorksetSnapshot,
    stage_snapshot,
)


def database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    return url


@pytest.fixture(autouse=True)
def clean_worksets(database_prerequisite):
    engine = create_engine(database_url())
    statement = text(
        "TRUNCATE work_parent_edges, workset_memberships, worksets, workset_cutovers, "
        "workset_authority, work_metadata_cutovers, work_metadata_authority, work_edges, "
        "work_authority_cutovers, work_authority, work_index, work_handles CASCADE"
    )
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


async def activate_read_authority(engine):
    async with engine.begin() as connection:
        for table in (work_authority, work_metadata_authority, workset_authority):
            await connection.execute(table.insert().values(
                scope="workspace", state="POSTGRES_AUTHORITY", generation=1,
            ))
        for table in (work_authority_cutovers, work_metadata_cutovers, workset_cutovers):
            await connection.execute(table.insert().values(scope="workspace", generation=1))


async def staged_controller(engine, *, activate=True):
    work_id, workset_id = uuid4(), uuid4()
    await admit(engine, work_id)
    async with engine.begin() as connection:
        await connection.execute(update(work_handles).where(
            work_handles.c.id == work_id
        ).values(provider_work_id="123"))
    await stage_snapshot(engine, WorksetSnapshot(
        worksets=(Workset(workset_id, "project.test", "Test", "PROJECT"),),
        memberships=(Membership(workset_id, work_id, "AUTHORITATIVE"),),
        parent_edges=(),
    ))
    if activate:
        await activate_read_authority(engine)
    provider = Provider()
    provider.canonical_ids.discard("123")
    return work_id, provider, Controller(
        LaunchAuthority(active_work_id=work_id), PostgresState(engine), {"asana": provider}
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


@pytest.mark.asyncio
async def test_reader_enumerates_current_workset_and_structure_from_db(database_prerequisite):
    engine = create_async_engine(database_url())
    master, child, terminal, role_set = uuid4(), uuid4(), uuid4(), uuid4()
    await admit(engine, master, child, terminal)
    async with engine.begin() as connection:
        await connection.execute(work_index.update().where(
            work_index.c.work_id == master).values(routing={
            "lifecycle_state": "CURRENT", "canonical_root": "NONE", "owner_key": "role",
            "wait_kind": "NONE", "unblock_condition": "NONE", "next_due": "NONE",
            "next_action_class": "IMPLEMENT", "next_action_ref": "next",
        }))
        await connection.execute(work_index.update().where(
            work_index.c.work_id == child).values(routing={
            "priority": "P1", "work_type": "Review", "lifecycle_state": "WAITING",
            "canonical_root": str(master), "owner_key": "reviewer", "wait_kind": "REVIEW",
            "unblock_condition": "terminal verdict", "next_due": "UNKNOWN",
            "next_action_class": "POLL", "next_action_ref": "review-1",
        }))
        await connection.execute(work_index.update().where(
            work_index.c.work_id == terminal).values(completed=True))
        await connection.execute(work_edges.insert().values(
            work_id=child, depends_on_work_id=master,
        ))
    await stage_snapshot(engine, WorksetSnapshot(
        worksets=(Workset(role_set, "agent.coordinator", "Coordinator", "DURABLE_ROLE",
                          "Coordinator"),),
        memberships=(
            Membership(role_set, master, "AUTHORITATIVE", "MASTER"),
            Membership(role_set, child, "AUTHORITATIVE"),
            Membership(role_set, terminal, "AUTHORITATIVE"),
        ), parent_edges=(ParentEdge(child, master),),
    ))
    reader = WorksetReader(engine)
    assert await reader.current(child) is None
    await activate_read_authority(engine)
    view = await reader.current(child)
    assert view is not None and view.workset.workset_key == "agent.coordinator"
    assert {member.item.id for member in view.members} == {master, child}
    waiting = next(member for member in view.members if member.item.id == child)
    assert waiting.depends_on == (master,)
    assert waiting.item.routing.unblock_condition == "terminal verdict"
    structure = await reader.structure(master)
    assert structure is not None and [item.id for item in structure.children] == [child]
    child_structure = await WorksetReader(engine).structure(child)
    assert child_structure is not None and child_structure.parent is not None
    assert child_structure.parent.id == master
    await engine.dispose()


@pytest.mark.asyncio
async def test_reader_fails_closed_on_partial_authority_marker(database_prerequisite):
    engine = create_async_engine(database_url())
    async with engine.begin() as connection:
        await connection.execute(workset_cutovers.insert().values(scope="workspace", generation=1))
    with pytest.raises(ValueError, match="inconsistent irreversible"):
        await WorksetReader(engine).generation()
    await engine.dispose()


@pytest.mark.asyncio
async def test_content_authority_replaces_provider_placement_only_after_marker(
    database_prerequisite,
):
    engine = create_async_engine(database_url())
    work_id, _provider, controller = await staged_controller(engine, activate=False)
    assert (await controller.get(WorkGetRequest(api_version="1", work_id=work_id))).status == "denied"

    await activate_read_authority(engine)
    current = await controller.get(WorkGetRequest(api_version="1", work_id=work_id))
    assert current.status == "ok" and current.item is not None
    assert current.item.notes == "initial notes" and current.item.revision.startswith("s3_")
    attachments = await controller.attachments(WorkAttachmentsRequest(
        api_version="1", work_id=work_id, observed_revision=current.item.revision,
    ))
    assert attachments.status == "ok" and attachments.attachments[0].name == "brief.txt"
    source = await controller.source_task(SourceTaskRequest(api_version="1", task_gid="123"))
    assert source.status == "ok" and source.item is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_content_authority_denies_missing_membership_and_foreign_provider_id(
    database_prerequisite,
):
    engine = create_async_engine(database_url())
    work_id, _provider, controller = await staged_controller(engine)
    async with engine.begin() as connection:
        await connection.execute(delete(workset_memberships).where(
            workset_memberships.c.work_id == work_id
        ))
    assert (await controller.get(WorkGetRequest(api_version="1", work_id=work_id))).status == "denied"
    foreign = await controller.source_task(SourceTaskRequest(api_version="1", task_gid="999"))
    assert foreign.status == "denied"
    await engine.dispose()


@pytest.mark.asyncio
async def test_content_authority_partial_marker_is_unknown(database_prerequisite):
    engine = create_async_engine(database_url())
    work_id, _provider, controller = await staged_controller(engine, activate=False)
    async with engine.begin() as connection:
        await connection.execute(workset_cutovers.insert().values(scope="workspace", generation=1))
    assert (await controller.get(WorkGetRequest(api_version="1", work_id=work_id))).status == "unknown"
    await engine.dispose()


@pytest.mark.asyncio
async def test_content_authority_token_change_during_read_returns_stale(database_prerequisite):
    engine = create_async_engine(database_url())
    work_id, provider, controller = await staged_controller(engine)
    current = await controller.get(WorkGetRequest(api_version="1", work_id=work_id))
    assert current.item is not None
    original = provider.list_attachments

    async def change_authorization(*args):
        async with engine.begin() as connection:
            await connection.execute(update(workset_memberships).where(
                workset_memberships.c.work_id == work_id
            ).values(row_version=2))
        return await original(*args)

    provider.list_attachments = change_authorization
    result = await controller.attachments(WorkAttachmentsRequest(
        api_version="1", work_id=work_id, observed_revision=current.item.revision,
    ))
    assert result.status == "stale" and result.revision != current.item.revision
    await engine.dispose()


@pytest.mark.asyncio
async def test_content_authority_token_change_during_work_get_returns_stale(
    database_prerequisite,
):
    engine = create_async_engine(database_url())
    work_id, provider, controller = await staged_controller(engine)
    original = provider.get

    async def change_authorization(*args):
        async with engine.begin() as connection:
            await connection.execute(update(workset_memberships).where(
                workset_memberships.c.work_id == work_id
            ).values(row_version=2))
        provider.get = original
        return await original(*args)

    provider.get = change_authorization
    result = await controller.get(WorkGetRequest(api_version="1", work_id=work_id))
    assert result.status == "stale" and result.item is not None
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
            text("alembic_version"))) == "0012_outcome_state"
    engine.dispose()
