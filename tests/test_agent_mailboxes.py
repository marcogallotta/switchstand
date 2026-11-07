import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.agent_mailboxes import (
    AgentMailboxState,
    agent_mailboxes,
    agent_name_key,
    chat_session_key,
)
from switchstand.state import metadata


@pytest.fixture
async def endpoints(database_prerequisite, role_takeover_password):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, activation_obligation_revisions"
        ))
        await connection.run_sync(metadata.create_all)
    yield AgentMailboxState(engine)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, activation_obligation_revisions"
        ))
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
    assert duplicate.status == "ok"
    assert duplicate.mailbox == alpha.mailbox


async def test_root_registration_requires_takeover_but_existing_session_resumes(endpoints):
    for alias in ("/root", "root", "ROOT", " / root "):
        denied = await endpoints.register_agent(alias, "owner", "unbound-chat")
        assert (denied.status, denied.reason) == (
            "denied", "reserved_name_requires_takeover",
        )
    async with endpoints.engine.begin() as connection:
        await connection.execute(insert(agent_mailboxes).values(
            name_key="root", display_name="/root", endpoint_id=uuid4(),
            principal_key="owner", session_key=chat_session_key("root-chat"), generation=4,
        ))
    await endpoints.register_agent("codex-head-generated", "other", "other-chat")
    resumed = await endpoints.register_agent("codex-head-generated", "owner", "root-chat")
    assert resumed.status == "ok"
    assert resumed.mailbox.name == "/root" and resumed.mailbox.generation == 4
    taken = await endpoints.takeover("/root", "owner", "replacement-root-chat")
    assert taken.status == "ok"
    assert taken.mailbox.endpoint_id == resumed.mailbox.endpoint_id
    assert taken.mailbox.generation == 5
    assert (await endpoints.for_actor("owner", "root-chat")).reason == "agent_not_registered"


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
    assert alpha.status == beta.status == "ok"
    assert alpha.mailbox == beta.mailbox


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


async def test_authorized_takeover_rebinds_principal_and_fences_old_chat(endpoints):
    created = await endpoints.register_agent("Lifecycle", "old-owner", "old-chat")

    taken = await endpoints.takeover(
        "Lifecycle", "new-owner", "new-chat", allow_principal_change=True,
    )

    assert taken.status == "ok" and taken.mailbox is not None
    assert taken.mailbox.endpoint_id == created.mailbox.endpoint_id
    assert taken.mailbox.generation == created.mailbox.generation + 1
    assert (await endpoints.for_actor("old-owner", "old-chat")).reason == "agent_not_registered"
    assert (await endpoints.for_actor("new-owner", "new-chat")).mailbox == taken.mailbox


async def test_cross_principal_transfer_requires_exact_host_approval(endpoints):
    created = await endpoints.register_agent("Coordinator", "old-owner", "old-chat")
    requested = await endpoints.request_transfer("Coordinator", "new-owner", "new-chat")
    assert requested.status == "pending" and requested.request_id is not None
    first, replay = await asyncio.gather(
        endpoints.approve_transfer(requested.request_id, password="synthetic-takeover-password"),
        endpoints.approve_transfer(requested.request_id, password="synthetic-takeover-password"),
    )
    assert first.status == replay.status == "approved" and first.generation == replay.generation == 2
    assert (await endpoints.for_actor("old-owner", "old-chat")).reason == "agent_not_registered"
    current = await endpoints.for_actor("new-owner", "new-chat")
    assert current.status == "ok" and current.mailbox.endpoint_id == created.mailbox.endpoint_id


async def test_cross_principal_transfer_rejects_stale_preimage(endpoints):
    await endpoints.register_agent("Coordinator", "old-owner", "old-chat")
    requested = await endpoints.request_transfer("Coordinator", "new-owner", "new-chat")
    assert requested.request_id is not None
    assert (await endpoints.takeover("Coordinator", "old-owner", "replacement", password="synthetic-takeover-password")).status == "ok"
    stale = await endpoints.approve_transfer(requested.request_id, password="synthetic-takeover-password")
    replay = await endpoints.approve_transfer(requested.request_id, password="synthetic-takeover-password")
    assert (stale.status, stale.reason, replay) == ("stale", "mailbox_changed", stale)
    assert (await endpoints.for_actor("new-owner", "new-chat")).reason == "agent_not_registered"


async def test_cross_principal_transfer_rejects_destination_session_collision(endpoints):
    await endpoints.register_agent("Coordinator", "old-owner", "old-chat")
    requested = await endpoints.request_transfer("Coordinator", "new-owner", "new-chat")
    assert requested.request_id is not None
    assert (await endpoints.register_agent("Other", "new-owner", "new-chat")).status == "ok"
    conflict = await endpoints.approve_transfer(requested.request_id, password="synthetic-takeover-password")
    replay = await endpoints.approve_transfer(requested.request_id, password="synthetic-takeover-password")
    assert (conflict.status, conflict.reason, replay) == (
        "conflict", "session_already_registered", conflict,
    )
    assert (await endpoints.for_actor("old-owner", "old-chat")).status == "ok"


async def test_runtime_type_tracks_registration_and_takeovers_both_directions(endpoints):
    from sqlalchemy import update
    registered = await endpoints.register_agent("Moving", "owner", "codex:first")
    assert registered.mailbox.agent_type == "codex"
    async with endpoints.engine.begin() as connection:
        await connection.execute(update(agent_mailboxes).values(agent_type=None))
    renewed = await endpoints.register_agent("Moving", "owner", "codex:first")
    assert renewed.mailbox.agent_type == "codex"
    for session, expected in [("claude:second", "claude"), ("codex:third", "codex"),
                               ("chat-fourth", "chatgpt")]:
        moved = await endpoints.takeover("Moving", "owner", session)
        assert moved.mailbox.endpoint_id == registered.mailbox.endpoint_id
        assert moved.mailbox.agent_type == expected
    async with endpoints.engine.begin() as connection:
        await connection.execute(update(agent_mailboxes).values(agent_type=None))
    renewed = await endpoints.takeover("Moving", "owner", "chat-fourth")
    assert renewed.mailbox.agent_type == "chatgpt"


async def test_old_registration_cannot_overwrite_uncommitted_takeover_type(endpoints):
    from sqlalchemy import update
    original = (await endpoints.register_agent("Racing", "owner", "codex:old")).mailbox
    async with endpoints.engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(update(agent_mailboxes).where(
            agent_mailboxes.c.endpoint_id == original.endpoint_id,
        ).values(session_key=chat_session_key("claude:new"), agent_type="claude", generation=2))
        old_registration = asyncio.create_task(
            endpoints.register_agent("Racing", "owner", "codex:old"))
        for _ in range(100):
            if await connection.scalar(text("SELECT count(*) FROM pg_locks WHERE NOT granted")):
                break
            await asyncio.sleep(.01)
        else:
            pytest.fail("registration did not reach binding lock")
        assert not old_registration.done(), "registration must wait for binding lock"
        await transaction.commit()
    assert (await old_registration).status == "conflict"
    current = (await endpoints.by_name("Racing")).mailbox
    assert current.agent_type == "claude" and current.generation == 2


@pytest.mark.parametrize("name", [" / INTERFACE ", " Coordinator "])
async def test_protected_takeover_fails_closed_and_preserves_binding(
    endpoints, role_takeover_password, monkeypatch, name,
):
    created = await endpoints.register_agent(name, "owner", "old-session")
    original = created.mailbox
    assert original is not None
    for principal in ("owner", "other"):
        for password in (None, "wrong-synthetic-password"):
            denied = await endpoints.takeover(
                name, principal, "replacement", allow_principal_change=True, password=password,
            )
            assert (denied.status, denied.reason) == ("denied", "takeover_password_required")
            assert (await endpoints.by_name(name)).mailbox == original
    setting = f"SWITCHSTAND_{original.name_key.upper()}_TAKEOVER_PASSWORD_HASH"
    encoded = os.environ[setting]
    for invalid in ("", "invalid-hash"):
        monkeypatch.setenv(setting, invalid)
        assert (await endpoints.takeover(
            name, "owner", "replacement", password=role_takeover_password,
        )).status == "denied"
        assert (await endpoints.by_name(name)).mailbox == original
    monkeypatch.setenv(setting, encoded)
    moved = await endpoints.takeover(
        name, "other", "replacement", allow_principal_change=True,
        password=role_takeover_password,
    )
    assert moved.status == "ok" and moved.mailbox is not None
    assert moved.mailbox.endpoint_id == original.endpoint_id
    assert moved.mailbox.generation == original.generation + 1
    assert (await endpoints.for_actor("owner", "old-session")).reason == "agent_not_registered"
    replay = await endpoints.takeover(name, "other", "replacement", password=role_takeover_password)
    assert replay.mailbox == moved.mailbox
    # Registration resumes; it cannot rename the protected endpoint or steal it.
    assert (await endpoints.register_agent("Alias", "other", "replacement")).mailbox == moved.mailbox
    assert (await endpoints.register_agent(name, "owner", "another-session")).status == "conflict"
    assert (await endpoints.takeover("Alias", "other", "replacement")).status == "conflict"
    assert (await endpoints.by_name(name)).mailbox == moved.mailbox


@pytest.mark.parametrize("name", ["Interface", "Coordinator"])
async def test_host_transfer_cannot_bypass_protected_takeover(endpoints, role_takeover_password, name):
    original = (await endpoints.register_agent(name, "owner", "old-session")).mailbox
    request = await endpoints.request_transfer(name, "other", "replacement")
    assert request.request_id is not None
    for password in (None, "wrong"):
        denied = await endpoints.approve_transfer(request.request_id, password=password)
        assert (denied.status, denied.reason) == ("denied", "takeover_password_required")
        assert (await endpoints.by_name(name)).mailbox == original
    approved = await endpoints.approve_transfer(request.request_id, password=role_takeover_password)
    assert approved.status == "approved" and approved.generation == 2
    assert (await endpoints.for_actor("owner", "old-session")).reason == "agent_not_registered"
