import os
from uuid import uuid4

import pytest
from fastmcp import Client
from sqlalchemy.ext.asyncio import create_async_engine

from chatgpt_fixture import grant
from switchstand.agents import AgentDirectory
from switchstand.chatgpt import ChatGPTService
from switchstand.chatgpt_mcp import build_chatgpt_server
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext
from switchstand.messages import MessageState
from switchstand.state import PostgresState, metadata


@pytest.fixture
async def pair():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    state, grants = PostgresState(engine), GrantState(engine)
    agents, messages = AgentDirectory(engine), MessageState(engine, grants)
    principals = [
        PrincipalContext(issuer="fixture", subject=f"agent-{i}", client_id="test", assurance="test")
        for i in range(2)
    ]
    works = [(await state.bind("asana", str(uuid4().int))).id for _ in range(2)]
    for principal, work in zip(principals, works, strict=True):
        await grants.issue(grant(
            principal=principal, active=work, scope="workspace",
            operations=frozenset({"message"}),
        ), None)
    services = []
    for principal in principals:
        async def resolve(p=principal): return p
        services.append(ChatGPTService(resolve, state, grants, {}, messages, agents=agents))
    yield services, agents
    await engine.dispose()


async def test_agent_name_message_request_reply_disposition_and_recover(pair):
    services, agents = pair
    tools = [
        dict(__import__("switchstand.chatgpt_mcp", fromlist=["build_ordinary_tools"])
             .build_ordinary_tools(service, session_generation=lambda i=i: f"session-{i}"))
        for i, service in enumerate(services)
    ]
    assert (await tools[0]["agent_register"]("1", "alpha")).status == "ok"
    assert (await tools[1]["agent_register"]("1", "beta")).status == "ok"

    sent = await tools[0]["agent_message_send"]("1", "beta", uuid4(), {"ask": "review"})
    assert sent.status == "ok" and sent.message is not None
    delivery = sent.message.delivery_id
    pending = await tools[1]["agent_message_pending"]("1")
    assert pending.messages and pending.messages[0].delivery_id == delivery
    assert (await tools[1]["agent_message_receive"]("1", delivery)).status == "ok"

    result_id = uuid4()
    result = await tools[1]["agent_message_result_send"](
        "1", delivery, result_id, {"answer": "done"}
    )
    assert result.status == "ok"
    disposed = await tools[1]["agent_message_disposition"]("1", delivery, result_id)
    assert disposed.status == "ok" and disposed.state == "DISPOSITIONED"

    second = await tools[0]["agent_message_send"]("1", "beta", uuid4(), {"ask": "again"})
    assert second.message is not None
    second_delivery = second.message.delivery_id
    assert (await tools[1]["agent_message_receive"]("1", second_delivery)).status == "ok"

    binding = await agents.resolve("beta")
    assert binding is not None
    principal = await services[1].principal()
    assert principal is not None
    await agents.takeover("beta", principal.key, "session-replacement", authorized=True)
    replacement = dict(__import__("switchstand.chatgpt_mcp", fromlist=["build_ordinary_tools"])
        .build_ordinary_tools(services[1], session_generation=lambda: "session-replacement"))
    assert (await replacement["agent_message_recover"]("1", second_delivery)).status == "ok"
    stale = await tools[1]["agent_message_recover"]("1", second_delivery)
    assert stale.status == "denied" and stale.reason == "agent_not_registered"
