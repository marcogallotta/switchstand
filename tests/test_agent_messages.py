import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
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
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
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
    tools = dict(build_ordinary_tools(
        service,
        session_generation=lambda: "legacy-session",
        agent_identity=lambda: session[0],
    ))
    yield tools, actor, session, owner, other, service
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    await engine.dispose()


async def test_same_principal_two_chats_survive_transport_churn_and_restart(agent_messaging):
    tools, actor, session, owner, _other, service = agent_messaging
    assert (await tools["agent_register"]("1", "Alpha")).status == "ok"
    session[0] = "chat-b"
    assert (await tools["agent_register"]("1", "Beta")).status == "ok"

    session[0] = "chat-a"
    sent = await tools["agent_message_send"]("1", "Beta", uuid4(), {"request": "review"})
    assert sent.status == "ok" and sent.message is not None
    assert sent.next_action is not None
    assert "bounded wait" in sent.next_action
    assert "hourly Scheduled watch" in sent.next_action
    delivery = sent.message.delivery_id

    # A fresh tool/server binding models a new MCP transport and process with the same chat metadata.
    restarted = dict(build_ordinary_tools(
        service,
        session_generation=lambda: "new-legacy-session",
        agent_identity=lambda: session[0],
    ))
    session[0] = "chat-b"
    assert (await restarted["agent_message_receive"]("1", delivery)).state == "RECEIVED"
    result_id = uuid4()
    reply = await restarted["agent_message_result_send"](
        "1", delivery, result_id, {"result": "pass"}
    )
    assert reply.status == "ok"
    assert reply.next_action is None
    assert (await restarted["agent_message_disposition"]("1", delivery, result_id)).state \
        == "DISPOSITIONED"

    session[0] = "chat-a"
    returned = await tools["agent_message_pending"]("1")
    assert [item.message_id for item in returned.messages] == [result_id]
    assert actor[0] == owner


async def test_request_replay_watch_follows_current_delivery_state(agent_messaging):
    tools, _actor, session, _owner, _other, _service = agent_messaging
    assert (await tools["agent_register"]("1", "Alpha")).status == "ok"
    session[0] = "chat-b"
    assert (await tools["agent_register"]("1", "Beta")).status == "ok"

    session[0] = "chat-a"
    request_id = uuid4()
    payload = {"request": "review"}
    sent = await tools["agent_message_send"]("1", "Beta", request_id, payload)
    assert sent.status == "ok" and sent.message.state == "AVAILABLE"
    assert sent.next_action is not None
    delivery = sent.message.delivery_id

    session[0] = "chat-b"
    assert (await tools["agent_message_receive"]("1", delivery)).state == "RECEIVED"
    session[0] = "chat-a"
    received_replay = await tools["agent_message_send"]("1", "Beta", request_id, payload)
    assert received_replay.status == "ok"
    assert received_replay.message.state == "RECEIVED"
    assert received_replay.next_action is not None

    session[0] = "chat-b"
    result_id = uuid4()
    assert (await tools["agent_message_result_send"](
        "1", delivery, result_id, {"result": "pass"}
    )).status == "ok"
    assert (await tools["agent_message_disposition"](
        "1", delivery, result_id
    )).state == "DISPOSITIONED"

    session[0] = "chat-a"
    completed_replay = await tools["agent_message_send"]("1", "Beta", request_id, payload)
    assert completed_replay.status == "ok"
    assert completed_replay.message.state == "DISPOSITIONED"
    assert completed_replay.next_action is None


async def test_missing_runtime_identity_is_local_to_agent_messaging(agent_messaging):
    tools, _actor, session, _owner, _other, service = agent_messaging
    session[0] = ""
    registration = await tools["agent_register"]("1", "Alpha")
    assert (registration.status, registration.reason) == (
        "recovery_required", "runtime_identity_unavailable",
    )
    # Unrelated ordinary tools remain registered and callable through their own admission paths.
    assert "work_get" in tools and "repository_bundle_get" in tools

    legacy_only = dict(build_ordinary_tools(
        service, session_generation=lambda: "transport-session"
    ))
    unavailable = await legacy_only["agent_register"]("1", "TransportFallback")
    assert (unavailable.status, unavailable.reason) == (
        "recovery_required", "runtime_identity_unavailable",
    )


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
    assert stale.next_action is None


async def test_takeover_atomically_fences_every_inflight_message_operation(
    agent_messaging, monkeypatch,
):
    tools, _actor, session, _owner, _other, service = agent_messaging
    messages = service.messages
    assert messages is not None
    assert (await tools["agent_register"]("1", "Alpha")).status == "ok"
    session[0] = "beta-1"
    assert (await tools["agent_register"]("1", "Beta")).status == "ok"

    async def cross_takeover(method_name, operation, replacement):
        original = getattr(messages, method_name)
        entered, release = asyncio.Event(), asyncio.Event()

        async def paused(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(messages, method_name, paused)
        stale_call = asyncio.create_task(operation())
        await entered.wait()
        session[0] = replacement
        name = "Beta" if replacement.startswith("beta") else "Alpha"
        takeover = await tools["agent_takeover"]("1", name)
        assert takeover.status == "ok"
        release.set()
        result = await stale_call
        monkeypatch.setattr(messages, method_name, original)
        return result

    session[0] = "chat-a"
    stale_send = await cross_takeover(
        "submit_admitted",
        lambda: tools["agent_message_send"]("1", "Beta", uuid4(), {"request": "stale"}),
        "alpha-2",
    )
    assert (stale_send.status, stale_send.reason) == ("stale", "sender_binding_changed")
    session[0] = "beta-1"
    assert not (await tools["agent_message_pending"]("1")).messages

    session[0] = "alpha-2"
    sent = await tools["agent_message_send"]("1", "Beta", uuid4(), {"request": "current"})
    assert sent.status == "ok" and sent.message is not None
    delivery = sent.message.delivery_id

    session[0] = "beta-1"
    stale_pending = await cross_takeover(
        "pending_admitted", lambda: tools["agent_message_pending"]("1"), "beta-2"
    )
    assert (stale_pending.status, stale_pending.reason) == (
        "stale", "receiving_binding_changed",
    )

    stale_receive = await cross_takeover(
        "receive_admitted",
        lambda: tools["agent_message_receive"]("1", delivery),
        "beta-3",
    )
    assert (stale_receive.status, stale_receive.reason) == (
        "stale", "receiving_binding_changed",
    )
    current_receive = await tools["agent_message_receive"]("1", delivery)
    assert (current_receive.status, current_receive.state) == ("ok", "RECEIVED")

    stale_recover = await cross_takeover(
        "recover_admitted",
        lambda: tools["agent_message_recover"]("1", delivery),
        "beta-4",
    )
    assert (stale_recover.status, stale_recover.reason) == (
        "stale", "receiving_binding_changed",
    )
    assert (await tools["agent_message_recover"]("1", delivery)).status == "ok"

    result_id = uuid4()
    stale_reply = await cross_takeover(
        "submit_received_result",
        lambda: tools["agent_message_result_send"](
            "1", delivery, result_id, {"result": "stale"}
        ),
        "beta-5",
    )
    assert (stale_reply.status, stale_reply.reason) == ("stale", "sender_binding_changed")
    session[0] = "alpha-2"
    assert not (await tools["agent_message_pending"]("1")).messages
    session[0] = "beta-5"
    assert (await tools["agent_message_recover"]("1", delivery)).status == "ok"
    result_id = uuid4()
    reply = await tools["agent_message_result_send"](
        "1", delivery, result_id, {"result": "current"}
    )
    assert reply.status == "ok"

    stale_disposition = await cross_takeover(
        "disposition_admitted",
        lambda: tools["agent_message_disposition"]("1", delivery, result_id),
        "beta-6",
    )
    assert (stale_disposition.status, stale_disposition.reason) == (
        "stale", "receiving_binding_changed",
    )
    assert (await tools["agent_message_recover"]("1", delivery)).status == "ok"
    assert (await tools["agent_message_disposition"]("1", delivery, result_id)).state \
        == "DISPOSITIONED"


async def test_logical_replay_survives_recipient_and_sender_takeover(agent_messaging):
    tools, _actor, session, _owner, _other, _service = agent_messaging
    assert (await tools["agent_register"]("1", "Alpha")).status == "ok"
    session[0] = "beta-1"
    assert (await tools["agent_register"]("1", "Beta")).status == "ok"

    session[0] = "chat-a"
    request_id = uuid4()
    first = await tools["agent_message_send"]("1", "Beta", request_id, {"request": "once"})
    assert first.status == "ok" and first.message is not None
    session[0] = "beta-2"
    assert (await tools["agent_takeover"]("1", "Beta")).status == "ok"
    session[0] = "chat-a"
    replay = await tools["agent_message_send"]("1", "Beta", request_id, {"request": "once"})
    assert replay == first

    session[0] = "beta-2"
    delivery = first.message.delivery_id
    assert (await tools["agent_message_receive"]("1", delivery)).status == "ok"
    result_id = uuid4()
    result = await tools["agent_message_result_send"](
        "1", delivery, result_id, {"result": "once"}
    )
    assert result.status == "ok"
    session[0] = "beta-3"
    assert (await tools["agent_takeover"]("1", "Beta")).status == "ok"
    result_replay = await tools["agent_message_result_send"](
        "1", delivery, result_id, {"result": "once"}
    )
    assert result_replay == result


async def test_message_transaction_linearizes_before_takeover(agent_messaging, monkeypatch):
    tools, _actor, session, _owner, _other, service = agent_messaging
    messages = service.messages
    assert messages is not None
    assert (await tools["agent_register"]("1", "Alpha")).status == "ok"
    session[0] = "beta-old"
    assert (await tools["agent_register"]("1", "Beta")).status == "ok"
    session[0] = "chat-a"
    sent = await tools["agent_message_send"]("1", "Beta", uuid4(), {"request": "lock"})
    assert sent.status == "ok" and sent.message is not None

    original = messages._current_agent_binding
    locked, release = asyncio.Event(), asyncio.Event()

    async def pause_with_row_locked(connection, binding):
        current = await original(connection, binding)
        locked.set()
        await release.wait()
        return current

    monkeypatch.setattr(messages, "_current_agent_binding", pause_with_row_locked)
    session[0] = "beta-old"
    receiving = asyncio.create_task(
        tools["agent_message_receive"]("1", sent.message.delivery_id)
    )
    await locked.wait()
    session[0] = "beta-new"
    takeover = asyncio.create_task(tools["agent_takeover"]("1", "Beta"))
    done, _ = await asyncio.wait({takeover}, timeout=0.1)
    assert not done
    release.set()
    assert (await receiving).status == "ok"
    assert (await takeover).status == "ok"
