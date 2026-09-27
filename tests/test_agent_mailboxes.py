import os

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.agent_mailboxes import (
    MAILBOX_PROVIDER,
    AgentMailboxState,
    agent_name_key,
)
from switchstand.state import PostgresState, metadata, work_handles


@pytest.fixture
async def subject():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.run_sync(metadata.create_all)
    state = PostgresState(engine)
    first = await state.bind("asana", "111")
    second = await state.bind("asana", "222")
    yield AgentMailboxState(engine), first.id, second.id
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
    await engine.dispose()


def test_agent_name_key_is_visible_name_collision_key():
    assert agent_name_key(" Main   Coordinator ") == "main-coordinator"
    assert agent_name_key("MAIN coordinator") == "main-coordinator"


async def test_registration_is_idempotent_but_rejects_name_and_principal_collisions(subject):
    mailboxes, first, second = subject
    created = await mailboxes.register("Main Coordinator", first, "principal-a")
    assert created.status == "ok"
    assert created.mailbox.name == "Main Coordinator"
    assert created.mailbox.work_id == first
    assert created.mailbox.generation == 1

    replay = await mailboxes.register("Main Coordinator", first, "principal-a")
    assert replay == created

    collision = await mailboxes.register("main coordinator", second, "principal-b")
    assert (collision.status, collision.reason) == ("conflict", "name_collision")

    duplicate = await mailboxes.register("Other Agent", second, "principal-a")
    assert (duplicate.status, duplicate.reason) == (
        "conflict", "principal_already_registered"
    )
    same_work = await mailboxes.register("Third Agent", first, "principal-c")
    assert (same_work.status, same_work.reason) == ("conflict", "work_already_registered")
    assert (await mailboxes.by_work_id(first)).mailbox == created.mailbox


async def test_takeover_preserves_mailbox_identity_and_fences_old_principal(subject):
    mailboxes, first, _second = subject
    created = await mailboxes.register("Lifecycle", first, "principal-old")
    assert created.mailbox is not None

    stale = await mailboxes.takeover("Lifecycle", 2, "principal-new")
    assert (stale.status, stale.reason) == ("conflict", "generation_changed")

    moved = await mailboxes.takeover("Lifecycle", 1, "principal-new")
    assert moved.status == "ok"
    assert moved.mailbox is not None
    assert moved.mailbox.work_id == first
    assert moved.mailbox.generation == 2
    assert (await mailboxes.for_principal("principal-old")).reason == "principal_not_registered"
    assert (await mailboxes.for_principal("principal-new")).mailbox == moved.mailbox
    assert (await mailboxes.by_name("lifecycle")).mailbox == moved.mailbox


async def test_agent_registration_uses_internal_mailbox_identity(subject):
    mailboxes, _first, _second = subject
    created = await mailboxes.register_agent("Agent Identity", "principal-agent")
    assert created.status == "ok" and created.mailbox is not None
    assert created.mailbox.generation == 1
    assert created.mailbox.work_id != _first
    async with mailboxes.engine.connect() as connection:
        row = (await connection.execute(
            select(work_handles.c.provider, work_handles.c.provider_work_id).where(
                work_handles.c.id == created.mailbox.work_id
            )
        )).one()
    assert row == (MAILBOX_PROVIDER, "agent-identity")
    assert await mailboxes.register_agent("Agent Identity", "principal-agent") == created
