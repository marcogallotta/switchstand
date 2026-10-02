import os
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from chatgpt_fixture import Provider
from sqlalchemy import create_engine, delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.contracts import (
    LaunchAuthority,
    SourceStoriesRequest,
    SourceTaskRequest,
    WorkAttachmentsRequest,
    WorkContext,
    WorkGetRequest,
    WorkHistoryRequest,
    WorksetRequest,
)
from switchstand.core import Controller, authoritative_revision
from switchstand.provider import PROJECT, AsanaProvider
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
    worksets,
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


def asana_history_provider(*, placement_changes=False):
    task_reads = 0

    def respond(request):
        nonlocal task_reads
        if request.url.path.endswith("/stories"):
            story = {"gid": "456", "target": {"gid": "123"}, "text": "history",
                     "created_at": "now", "resource_subtype": "comment_added"}
            return httpx.Response(200, json={"data": [story], "next_page": None})
        task_reads += 1
        canonical = placement_changes and task_reads == 1
        membership = [{"project": {"gid": PROJECT, "name": "Area"}, "section": None}]
        task = {"gid": "123", "name": "Task", "notes": "Notes", "completed": False,
                "modified_at": "r1", "memberships": membership if canonical else [],
                "assignee": None, "parent": None, "custom_fields": []}
        return httpx.Response(200, json={"data": task})

    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", transport=httpx.MockTransport(respond)
    )
    return AsanaProvider(client), client


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
    assert view.revision.startswith("s3w_")
    assert await reader.enumerate(workset_key="agent.coordinator") == view
    assert {member.item.id for member in view.members} == {master, child}
    waiting = next(member for member in view.members if member.item.id == child)
    assert waiting.depends_on == (master,)
    assert waiting.item.routing.unblock_condition == "terminal verdict"
    structure = await reader.structure(master)
    assert structure is not None and [item.id for item in structure.children] == [child]
    child_structure = await WorksetReader(engine).structure(child)
    assert child_structure is not None and child_structure.parent is not None
    assert child_structure.parent.id == master
    before = view.revision
    async with engine.begin() as connection:
        await connection.execute(update(workset_memberships).where(
            workset_memberships.c.work_id == terminal,
        ).values(row_version=2))
    revised = await reader.enumerate(workset_id=role_set)
    assert revised is not None and revised.revision != before
    with pytest.raises(ValueError, match="exactly one"):
        await reader.enumerate()
    with pytest.raises(ValueError, match="exactly one"):
        WorksetRequest(api_version="1", workset_id=role_set,
                       workset_key="agent.coordinator")
    await engine.dispose()


@pytest.mark.asyncio
async def test_parent_mutation_advances_revision_and_rejects_cycles(database_prerequisite):
    engine = create_async_engine(database_url())
    first, second, third, project = uuid4(), uuid4(), uuid4(), uuid4()
    await admit(engine, first, second, third)
    await stage_snapshot(engine, WorksetSnapshot(
        worksets=(Workset(project, "project.test", "Test", "PROJECT"),),
        memberships=tuple(
            Membership(project, work_id, "AUTHORITATIVE")
            for work_id in (first, second, third)
        ),
        parent_edges=(ParentEdge(second, first),),
    ))
    await activate_read_authority(engine)
    reader = WorksetReader(engine)

    assert await reader.update_parent(third, second, 1)
    assert await reader.parent_matches(third, second)
    async with engine.connect() as connection:
        version = await connection.scalar(select(work_index.c.row_version).where(
            work_index.c.work_id == third
        ))
    assert version == 2
    assert not await reader.update_parent(third, first, 1)
    assert await reader.parent_matches(third, second)
    with pytest.raises(ValueError, match="cycle"):
        await reader.validate_parent(first, third)
    with pytest.raises(ValueError, match="itself"):
        await reader.validate_parent(first, first)

    assert await reader.update_parent(third, None, 2)
    assert await reader.parent_matches(third, None)
    await engine.dispose()


@pytest.mark.asyncio
async def test_general_workset_and_membership_mutations_preserve_invariants(
    database_prerequisite,
):
    engine = create_async_engine(database_url())
    master, successor, moving = uuid4(), uuid4(), uuid4()
    role_set, first_set, second_set = uuid4(), uuid4(), uuid4()
    await admit(engine, master, successor, moving)
    await stage_snapshot(engine, WorksetSnapshot(
        worksets=(
            Workset(role_set, "agent.coordinator", "Coordinator", "DURABLE_ROLE",
                    "Coordinator"),
            Workset(first_set, "project.first", "First", "PROJECT"),
            Workset(second_set, "project.second", "Second", "PROJECT"),
        ),
        memberships=(
            Membership(role_set, master, "AUTHORITATIVE", "MASTER"),
            Membership(role_set, successor, "AUTHORITATIVE"),
            Membership(first_set, moving, "AUTHORITATIVE"),
        ),
        parent_edges=(),
    ))
    await activate_read_authority(engine)
    reader = WorksetReader(engine)

    first_revision = (await reader.enumerate(workset_id=first_set)).revision
    assert await reader.update_workset(first_set, 1, name="Renamed")
    assert await reader.update_workset(first_set, 1, name="Renamed")
    assert not await reader.update_workset(first_set, 1, state="RETIRED")
    assert await reader.update_workset(first_set, 2, state="RETIRED")
    with pytest.raises(ValueError, match="not active"):
        await reader.update_related_membership(
            successor, first_set, add=True, expected_workset_version=3,
        )
    assert await reader.update_workset(first_set, 3, state="ACTIVE")
    current = await reader.enumerate(workset_key="project.first")
    assert current is not None and current.workset.name == "Renamed"
    assert current.revision != first_revision
    assert await reader.update_related_membership(
        successor, first_set, add=True, expected_workset_version=4,
    )
    assert await reader.update_related_membership(
        successor, first_set, add=True, expected_workset_version=4,
    )
    assert await reader.update_related_membership(
        successor, first_set, add=False, expected_workset_version=5,
    )
    assert await reader.update_related_membership(
        successor, first_set, add=False, expected_workset_version=5,
    )
    assert not await reader.update_related_membership(
        successor, first_set, add=True, expected_workset_version=4,
    )
    assert await reader.update_related_membership(
        successor, first_set, add=True, expected_workset_version=6,
    )
    assert not await reader.update_related_membership(
        successor, first_set, add=False, expected_workset_version=5,
    )
    assert await reader.update_related_membership(
        successor, first_set, add=False, expected_workset_version=7,
    )

    before_move = await reader.content_authorization(moving)
    assert await reader.move_authoritative_membership(
        moving, first_set, second_set, 1, None,
    )
    assert await reader.content_authorization(moving) != before_move
    async with engine.connect() as connection:
        moved = (await connection.execute(select(
            workset_memberships.c.workset_id,
            workset_memberships.c.semantics,
            workset_memberships.c.row_version,
        ).where(workset_memberships.c.work_id == moving).order_by(
            workset_memberships.c.workset_id,
        ))).all()
    assert {(row[0], row[1], row[2]) for row in moved} == {
        (first_set, "RELATED", 2), (second_set, "AUTHORITATIVE", 1),
    }
    assert await reader.move_authoritative_membership(
        moving, first_set, second_set, 1, None,
    )
    assert await reader.update_related_membership(
        moving, first_set, add=False, expected_workset_version=8,
    )
    with pytest.raises(ValueError, match="authoritative"):
        await reader.update_related_membership(
            moving, second_set, add=False, expected_workset_version=1,
        )

    before_master = await reader.content_authorization(master)
    before_successor = await reader.content_authorization(successor)
    assert await reader.transfer_master(role_set, master, successor, 1, 1)
    assert await reader.transfer_master(role_set, master, successor, 1, 1)
    assert await reader.content_authorization(master) != before_master
    assert await reader.content_authorization(successor) != before_successor
    async with engine.connect() as connection:
        roles = dict((await connection.execute(select(
            workset_memberships.c.work_id, workset_memberships.c.member_role,
        ).where(workset_memberships.c.workset_id == role_set))).all())
    assert roles == {master: "MEMBER", successor: "MASTER"}
    assert not await reader.transfer_master(role_set, successor, master, 1, 1)
    with pytest.raises(ValueError, match="movable"):
        await reader.move_authoritative_membership(
            successor, role_set, first_set, 2, None,
        )
    with pytest.raises(ValueError, match="change name or state"):
        await reader.update_workset(first_set, 4)

    async with engine.connect() as connection:
        assert await connection.scalar(select(worksets.c.row_version).where(
            worksets.c.workset_id == first_set,
        )) == 9
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
async def test_content_authority_controls_real_provider_history(database_prerequisite):
    engine = create_async_engine(database_url())
    work_id, _, _ = await staged_controller(engine)
    state = PostgresState(engine)
    provider, client = asana_history_provider()
    controller = Controller(LaunchAuthority(active_work_id=work_id), state, {"asana": provider})
    revision = await authoritative_revision(
        state, work_id, "r1", provider_notes="Notes", provider_context=WorkContext()
    )
    history = await controller.history(WorkHistoryRequest(
        api_version="1", work_id=work_id, observed_revision=revision,
    ))
    assert history.status == "ok" and history.events[0].text == "history"
    await client.aclose()

    provider, client = asana_history_provider(placement_changes=True)
    controller = Controller(LaunchAuthority(active_work_id=work_id), state, {"asana": provider})
    source = await controller.source_stories(SourceStoriesRequest(
        api_version="1", task_gid="123", observed_revision="r1",
    ))
    assert source.status == "ok" and source.stories[0].text == "history"
    await client.aclose()
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
