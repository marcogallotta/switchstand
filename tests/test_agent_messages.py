import os
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.chatgpt import ChatGPTService
from switchstand.chatgpt_mcp import build_ordinary_tools
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext
from switchstand.messages import MessageState
from switchstand.state import PostgresState, metadata


@pytest.fixture
async def agent_messaging(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.run_sync(metadata.create_all)
    owner = PrincipalContext(issuer="fixture", subject="owner", client_id="test", assurance="test")
    other = PrincipalContext(issuer="fixture", subject="other", client_id="test", assurance="test")
    actor, session = [owner], ["chat-a"]

    async def resolve():
        return actor[0]

    service = ChatGPTService(
        resolve, PostgresState(engine), GrantState(engine), {},
        MessageState(engine, GrantState(engine)),
    )
    tools = dict(build_ordinary_tools(service, session_generation=lambda: session[0]))
    yield tools, actor, session, owner, other, service
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
    await engine.dispose()


async def test_same_principal_two_chats_survive_transport_churn_and_restart(agent_messaging):
    tools, actor, session, owner, _other, service = agent_messaging
    assert (await tools["agent_register"]("1", "Alpha")).status == "ok"
    session[0] = "chat-b"
    assert (await tools["agent_register"]("1", "Beta")).status == "ok"

    session[0] = "chat-a"
    sent = await tools["agent_message_send"]("1", "Beta", uuid4(), {"request": "review"})
    assert sent.status == "ok" and sent.message is not None
    delivery = sent.message.delivery_id

    # A fresh tool/server binding models a new MCP transport and process with the same chat metadata.
    restarted = dict(build_ordinary_tools(service, session_generation=lambda: session[0]))
    session[0] = "chat-b"
    assert (await restarted["agent_message_receive"]("1", delivery)).state == "RECEIVED"
    result_id = uuid4()
    reply = await restarted["agent_message_result_send"](
        "1", delivery, result_id, {"result": "pass"}
    )
    assert reply.status == "ok"
    assert (await restarted["agent_message_disposition"]("1", delivery, result_id)).state \
        == "DISPOSITIONED"

    session[0] = "chat-a"
    returned = await tools["agent_message_pending"]("1")
    assert [item.message_id for item in returned.messages] == [result_id]
    assert actor[0] == owner


async def test_missing_runtime_identity_is_local_to_agent_messaging(agent_messaging):
    tools, _actor, session, _owner, _other, _service = agent_messaging
    session[0] = ""
    registration = await tools["agent_register"]("1", "Alpha")
    assert (registration.status, registration.reason) == (
        "recovery_required", "runtime_identity_unavailable",
    )
    # Unrelated ordinary tools remain registered and callable through their own admission paths.
    assert "work_get" in tools and "repository_bundle_get" in tools


async def test_takeover_preserves_delivery_and_fences_old_session(agent_messaging):
    tools, actor, session, _owner, other, _service = agent_messaging
    assert (await tools["agent_register"]("1", "Alpha")).status == "ok"
    session[0] = "chat-b"
    assert (await tools["agent_register"]("1", "Beta")).status == "ok"
    session[0] = "chat-a"
    sent = await tools["agent_message_send"]("1", "Beta", uuid4(), {"request": "handoff"})
    delivery = sent.message.delivery_id
    session[0] = "chat-b"
    assert (await tools["agent_message_receive"]("1", delivery)).state == "RECEIVED"

    actor[0] = other
    session[0] = "other-chat"
    denied = await tools["agent_takeover"]("1", "Beta")
    assert (denied.status, denied.reason) == ("denied", "principal_mismatch")

    actor[0] = _owner
    session[0] = "replacement"
    assert (await tools["agent_takeover"]("1", "Beta")).status == "ok"
    recovered = await tools["agent_message_recover"]("1", delivery)
    assert recovered.status == "ok" and recovered.state == "RECEIVED"

    # An ambiguous takeover response may be retried. It must preserve the durable
    # generation so this already-recovered delivery remains replyable.
    assert (await tools["agent_takeover"]("1", "Beta")).status == "ok"
    replied = await tools["agent_message_result_send"](
        "1", delivery, uuid4(), {"result": "replacement"},
    )
    assert replied.status == "ok"

    session[0] = "chat-b"
    stale = await tools["agent_message_result_send"]("1", delivery, uuid4(), {"result": "old"})
    assert (stale.status, stale.reason) == ("denied", "agent_not_registered")
