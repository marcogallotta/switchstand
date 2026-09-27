import os
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.agents import AgentDirectory
from switchstand.state import PostgresState, metadata


@pytest.fixture
async def directory():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    subject = AgentDirectory(engine)
    state = PostgresState(engine)
    yield subject, state
    await engine.dispose()


async def test_register_is_idempotent_and_name_is_immutable(directory):
    directory, state = directory
    mailbox = (await state.bind("asana", str(uuid4().int))).id
    first = await directory.register("principal-a", "session-a", "agent-a", mailbox)
    assert first.status == "ok" and first.identity is not None
    assert (await directory.register(
        "principal-a", "session-a", "agent-a", mailbox
    )) == first
    conflict = await directory.register("principal-a", "session-a", "other", mailbox)
    assert conflict.status == "conflict" and conflict.reason == "session_already_registered"


async def test_name_collision_and_authorized_takeover_preserve_mailbox(directory):
    directory, state = directory
    mailbox = (await state.bind("asana", str(uuid4().int))).id
    first = await directory.register("principal-a", "session-a", "agent-a", mailbox)
    assert first.identity is not None
    other_mailbox = (await state.bind("asana", str(uuid4().int))).id
    collision = await directory.register("principal-b", "session-b", "agent-a", other_mailbox)
    assert collision.status == "conflict" and collision.reason == "name_unavailable"
    with pytest.raises(PermissionError):
        await directory.takeover("agent-a", "principal-b", "session-b")
    moved = await directory.takeover(
        "agent-a", "principal-b", "session-b", authorized=True
    )
    assert moved.status == "ok" and moved.identity is not None
    assert moved.identity.agent_id == first.identity.agent_id
    assert moved.identity.binding_generation == 2
    binding = await directory.resolve("agent-a")
    assert binding is not None and binding.mailbox_work_id == mailbox
    assert binding.principal_key == "principal-b"
