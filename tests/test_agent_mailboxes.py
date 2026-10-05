import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.agent_mailboxes import AgentMailboxState, agent_name_key
from switchstand.state import metadata


@pytest.fixture
async def endpoints(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await connection.run_sync(metadata.create_all)
    yield AgentMailboxState(engine)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    await engine.dispose()


def test_agent_name_key_is_visible_name_collision_key():
    assert agent_name_key(" Main   Coordinator ") == "main-coordinator"
    assert agent_name_key("MAIN coordinator") == "main-coordinator"


async def test_same_principal_distinct_chats_own_distinct_names(endpoints):
    alpha = await endpoints.register_agent("Alpha", "owner", "chat-a")
    beta = await endpoints.register_agent("Beta", "owner", "chat-b")
    assert alpha.status == beta.status == "ok"
    assert alpha.mailbox.endpoint_id != beta.mailbox.endpoint_id
    assert (await endpoints.for_actor("owner", "chat-a")).mailbox == alpha.mailbox
    duplicate = await endpoints.register_agent("Gamma", "owner", "chat-a")
    assert (duplicate.status, duplicate.reason) == ("conflict", "session_already_registered")


async def test_concurrent_registration_reconciles_unique_identity(endpoints):
    first, replay = await asyncio.gather(
        endpoints.register_agent("Concurrent", "owner", "same-chat"),
        endpoints.register_agent("Concurrent", "owner", "same-chat"),
    )
    assert first.status == replay.status == "ok"
    assert first.mailbox == replay.mailbox

    alpha, beta = await asyncio.gather(
        endpoints.register_agent("Alpha", "other", "contended-chat"),
        endpoints.register_agent("Beta", "other", "contended-chat"),
    )
    assert sorted((alpha.status, beta.status)) == ["conflict", "ok"]
    conflict = alpha if alpha.status == "conflict" else beta
    assert conflict.reason == "session_already_registered"


async def test_takeover_is_same_owner_atomic_and_fences_old_chat(endpoints):
    created = await endpoints.register_agent("Lifecycle", "owner", "old-chat")
    denied = await endpoints.takeover("Lifecycle", "other", "new-chat")
    assert (denied.status, denied.reason) == ("denied", "principal_mismatch")

    first, second = await asyncio.gather(
        endpoints.takeover("Lifecycle", "owner", "replacement-a"),
        endpoints.takeover("Lifecycle", "owner", "replacement-b"),
    )
    assert first.status == second.status == "ok"
    current = (await endpoints.by_name("Lifecycle")).mailbox
    assert current is not None and current.generation == 3
    assert current.endpoint_id == created.mailbox.endpoint_id
    assert (await endpoints.for_actor("owner", "old-chat")).reason == "agent_not_registered"
    winner = "replacement-a" if (
        await endpoints.for_actor("owner", "replacement-a")
    ).status == "ok" else "replacement-b"
    assert (await endpoints.for_actor("owner", winner)).mailbox == current

    replay = await endpoints.takeover("Lifecycle", "owner", winner)
    assert replay.status == "ok" and replay.mailbox == current


async def test_cross_principal_transfer_requires_exact_host_approval(endpoints):
    created = await endpoints.register_agent("Root", "old-owner", "old-chat")
    requested = await endpoints.request_transfer("Root", "new-owner", "new-chat")
    assert requested.status == "pending" and requested.request_id is not None
    first, replay = await asyncio.gather(
        endpoints.approve_transfer(requested.request_id),
        endpoints.approve_transfer(requested.request_id),
    )
    assert first.status == replay.status == "approved" and first.generation == replay.generation == 2
    assert (await endpoints.for_actor("old-owner", "old-chat")).reason == "agent_not_registered"
    current = await endpoints.for_actor("new-owner", "new-chat")
    assert current.status == "ok" and current.mailbox.endpoint_id == created.mailbox.endpoint_id


async def test_cross_principal_transfer_rejects_stale_preimage(endpoints):
    await endpoints.register_agent("Root", "old-owner", "old-chat")
    requested = await endpoints.request_transfer("Root", "new-owner", "new-chat")
    assert requested.request_id is not None
    assert (await endpoints.takeover("Root", "old-owner", "replacement")).status == "ok"
    stale = await endpoints.approve_transfer(requested.request_id)
    replay = await endpoints.approve_transfer(requested.request_id)
    assert (stale.status, stale.reason, replay) == ("stale", "mailbox_changed", stale)
    assert (await endpoints.for_actor("new-owner", "new-chat")).reason == "agent_not_registered"


async def test_cross_principal_transfer_rejects_destination_session_collision(endpoints):
    await endpoints.register_agent("Root", "old-owner", "old-chat")
    requested = await endpoints.request_transfer("Root", "new-owner", "new-chat")
    assert requested.request_id is not None
    assert (await endpoints.register_agent("Other", "new-owner", "new-chat")).status == "ok"
    conflict = await endpoints.approve_transfer(requested.request_id)
    replay = await endpoints.approve_transfer(requested.request_id)
    assert (conflict.status, conflict.reason, replay) == (
        "conflict", "session_already_registered", conflict,
    )
    assert (await endpoints.for_actor("old-owner", "old-chat")).status == "ok"
