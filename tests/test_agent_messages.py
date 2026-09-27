import os
from uuid import uuid4

from chatgpt_fixture import grant
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from switchstand.chatgpt import ChatGPTService
from switchstand.chatgpt_mcp import build_ordinary_tools
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext
from switchstand.messages import MessageState
from switchstand.state import PostgresState, metadata


@pytest.fixture
async def agent_messaging():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    state, grants = PostgresState(engine), GrantState(engine)
    principals = [
        PrincipalContext(
            issuer="fixture", subject=str(uuid4()), client_id="test", assurance="test"
        )
        for _ in range(2)
    ]
    works = [(await state.bind("asana", str(uuid4().int))).id for _ in range(2)]
    for principal, work in zip(principals, works, strict=True):
        await grants.issue(
            grant(
                principal=principal, active=work,
                operations=frozenset({"message"}),
            ),
            None,
        )
    actor = [principals[0]]

    async def resolve():
        return actor[0]

    service = ChatGPTService(resolve, state, grants, {}, MessageState(engine, grants))
    tools = dict(build_ordinary_tools(service, session_generation=lambda: "session-1"))
    yield tools, actor, principals
    await engine.dispose()


async def test_agent_name_vertical_hides_workids_and_preserves_message_state(agent_messaging):
    tools, actor, principals = agent_messaging
    assert (await tools["agent_register"]("1", "Agent Alpha")).status == "ok"
    actor[0] = principals[1]
    assert (await tools["agent_register"]("1", "Agent Beta")).status == "ok"

    actor[0] = principals[0]
    sent = await tools["agent_message_send"](
        "1", "Agent Beta", uuid4(), {"request": "review"}
    )
    assert sent.status == "ok"
    assert sent.message.sender_name == "Agent Alpha"
    assert sent.message.recipient_name == "Agent Beta"
    assert "work_id" not in sent.message.model_dump()

    actor[0] = principals[1]
    pending = await tools["agent_message_pending"]("1")
    assert pending.status == "ok" and len(pending.messages) == 1
    delivery = pending.messages[0].delivery_id
    assert (await tools["agent_message_receive"]("1", delivery)).state == "RECEIVED"

    result_id = uuid4()
    reply = await tools["agent_message_result_send"](
        "1", delivery, result_id, {"result": "pass"}
    )
    assert reply.status == "ok"
    assert reply.message.sender_name == "Agent Beta"
    assert reply.message.recipient_name == "Agent Alpha"
    disposition = await tools["agent_message_disposition"]("1", delivery, result_id)
    assert disposition.state == "DISPOSITIONED"

    actor[0] = principals[0]
    returned = await tools["agent_message_pending"]("1")
    assert returned.status == "ok"
    assert [item.message_id for item in returned.messages] == [result_id]


async def test_agent_name_collision_and_unregistered_sender_are_closed(agent_messaging):
    tools, actor, principals = agent_messaging
    assert (await tools["agent_register"]("1", "Lifecycle")).status == "ok"
    actor[0] = principals[1]
    collision = await tools["agent_register"]("1", "LIFECYCLE")
    assert (collision.status, collision.reason) == ("conflict", "name_collision")
    denied = await tools["agent_message_send"](
        "1", "Lifecycle", uuid4(), {"request": "x"}
    )
    assert (denied.status, denied.reason) == ("denied", "agent_not_registered")
