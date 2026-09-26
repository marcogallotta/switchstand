import asyncio
import os
import time
from uuid import uuid4

import httpx2
import pytest
from chatgpt_fixture import grant
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AccessToken
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand import chatgpt_edge
from switchstand.chatgpt import ChatGPTService
from switchstand.chatgpt_mcp import build_chatgpt_server
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext
from switchstand.messages import (
    MessagePendingRequest,
    MessageSendRequest,
    MessageState,
    message_deliveries,
    message_projection,
    messages,
)
from switchstand.state import PostgresState, metadata


@pytest.fixture
async def messaging():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL message service tests")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    state, grants = PostgresState(engine), GrantState(engine)
    principals = [PrincipalContext(issuer="fixture", subject=str(uuid4()), client_id="test", assurance="test") for _ in range(2)]
    works = [(await state.bind("asana", str(uuid4().int))).id for _ in range(2)]
    issued = [grant(principal=p, active=w, operations=frozenset({"message"})) for p, w in zip(principals, works, strict=True)]
    for selected in issued:
        await grants.issue(selected, None)
    actor = [principals[0]]
    async def resolve(): return actor[0]
    service = ChatGPTService(resolve, state, grants, {}, MessageState(engine, grants))
    yield service, actor, principals, works, issued
    await engine.dispose()


def send(work, version, recipient, **changes):
    values = {"api_version": "1", "work_id": work, "grant_version": version,
              "message_id": uuid4(), "route_ref": "review", "payload": {"ok": True},
              "recipient_work_id": recipient}
    return MessageSendRequest(**(values | changes))


async def row_counts(service):
    async with service.messages.engine.connect() as connection:
        count = select(func.count()).select_from
        return tuple([await connection.scalar(count(table)) for table in (
            messages, message_deliveries, message_projection,
        )])


async def test_service_vertical_correlation_and_truthful_reply_failure(messaging, monkeypatch):
    service, actor, principals, works, _issued = messaging
    first = await service.message_send(send(works[0], 1, works[1]))
    assert first.status == "ok"
    actor[0] = principals[1]
    pending = await service.message_pending(works[1], MessagePendingRequest(
        api_version="1", grant_version=1))
    assert pending.messages == (first.message,)
    result = await service.message_send(MessageSendRequest(
        api_version="1", work_id=works[1], grant_version=1, message_id=uuid4(),
        payload={"result": "done"}, in_reply_to_delivery_id=first.message.delivery_id))
    assert result.status == "ok" and result.message.route_ref == "review"
    actor[0] = principals[0]
    assert (await service.message_pending(works[0], MessagePendingRequest(
        api_version="1", grant_version=1))).messages == (result.message,)
    missing = send(works[0], 1, None, route_ref=None, recipient_work_id=None,
                   in_reply_to_delivery_id=uuid4())
    assert (await service.message_send(missing)).reason == "reply_delivery_not_found"
    async def failed(_delivery): raise SQLAlchemyError("lookup failed")
    monkeypatch.setattr(service.messages, "reply_context", failed)
    assert (await service.message_send(missing)).reason == "state_unavailable"


async def test_new_recipient_grant_waits_for_send_and_reciprocal_send(messaging, monkeypatch):
    service, _actor, principals, works, _issued = messaging
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.messages.submit_admitted
    async def held(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    monkeypatch.setattr(service.messages, "submit_admitted", held)
    sending = asyncio.create_task(service.message_send(send(works[0], 1, works[1])))
    await entered.wait()
    duplicate = grant(principal=PrincipalContext(
        issuer="fixture", subject=str(uuid4()), client_id="test", assurance="test"),
        active=works[1], operations=frozenset({"message"}))
    issuance = asyncio.create_task(service.grants.issue(duplicate, None))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(issuance), 0.05)
    assert await service.grants.current(duplicate.principal.key) is None
    release.set()
    assert (await asyncio.wait_for(sending, 2)).status == "ok"
    await asyncio.wait_for(issuance, 2)
    monkeypatch.setattr(service.messages, "submit_admitted", original)
    assert (await service.message_send(send(works[0], 1, works[1]))).reason == "recipient_route_unavailable"
    await service.grants.issue(duplicate.model_copy(update={
        "version": 2, "id": uuid4(), "operations": frozenset({"work_get"}),
    }), 1)
    async def resolve_b(): return principals[1]
    service_b = ChatGPTService(resolve_b, service.state, service.grants, {}, service.messages)
    outcomes = await asyncio.wait_for(asyncio.gather(
        service.message_send(send(works[0], 1, works[1])),
        service_b.message_send(send(works[1], 1, works[0])),
    ), 2)
    assert all(outcome.status == "ok" for outcome in outcomes)


async def test_committed_public_replay_survives_recipient_change(messaging):
    service, _actor, _principals, works, issued = messaging
    request = send(works[0], 1, works[1])
    first = await service.message_send(request)
    assert first.status == "ok"
    before = await row_counts(service)
    recipient = issued[1].model_copy(update={"version": 2, "id": uuid4()})
    await service.grants.issue(recipient, 1)
    assert await service.message_send(request) == first
    recipient = recipient.model_copy(update={
        "version": 3, "id": uuid4(), "operations": frozenset({"work_get"}),
    })
    await service.grants.issue(recipient, 2)
    assert await service.message_send(request) == first
    variants = [
        request.model_copy(update={"payload": {"ok": False}}),
        request.model_copy(update={"recipient_work_id": works[0]}),
        request.model_copy(update={"route_ref": "changed"}),
        request.model_copy(update={
            "recipient_work_id": None, "route_ref": None,
            "in_reply_to_delivery_id": uuid4(),
        }),
    ]
    for changed in variants:
        conflict = await service.message_send(changed)
        assert conflict.status == "conflict" and conflict.reason == "message_identity_conflict"
    novel = send(works[0], 1, works[1])
    assert (await service.message_send(novel)).reason == "recipient_route_unavailable"
    assert await row_counts(service) == before


async def test_actor_capability_recipient_and_route_contract_denials(messaging):
    service, _actor, _principals, works, issued = messaging
    before = await row_counts(service)
    cases = [send(uuid4(), 1, works[1]), send(works[0], 2, works[1])]
    for request, reason in zip(cases, ("actor_not_admitted", "grant_version_changed"), strict=True):
        result = await service.message_send(request)
        assert result.reason == reason and result.message is None
    no_message = issued[0].model_copy(update={"version": 2, "id": uuid4(),
                                             "operations": frozenset({"work_get"})})
    await service.grants.issue(no_message, 1)
    assert (await service.message_send(send(works[0], 2, works[1]))).reason == "message_not_granted"
    assert await row_counts(service) == before
    for changes in ({"route_ref": None}, {"in_reply_to_delivery_id": uuid4()}, {"recipient_work_id": None}):
        with pytest.raises(ValidationError):
            send(works[0], 2, works[1], **changes)


async def test_recipient_selection_accepts_workspace_and_detects_ambiguity(messaging):
    service, _actor, _principals, works, issued = messaging
    workspace = grant(principal=PrincipalContext(issuer="fixture", subject=str(uuid4()),
        client_id="test", assurance="test"), active=works[1], scope="workspace",
        operations=frozenset({"message"}))
    await service.grants.issue(workspace, None)
    ambiguous = await service.message_send(send(works[0], 1, works[1]))
    assert ambiguous.status == "denied" and ambiguous.reason == "recipient_route_unavailable"
    unavailable = issued[1].model_copy(update={"version": 2, "id": uuid4(),
        "operations": frozenset({"work_get"})})
    await service.grants.issue(unavailable, 1)
    assert (await service.message_send(send(works[0], 1, works[1]))).status == "ok"
    restored = unavailable.model_copy(update={"version": 3, "id": uuid4(), "operations": frozenset({"message"})})
    await service.grants.issue(restored, 2)
    duplicate = grant(principal=PrincipalContext(
        issuer="fixture", subject=str(uuid4()), client_id="test", assurance="test"),
        active=works[1], operations=frozenset({"message"}))
    await service.grants.issue(duplicate, None)
    denied = await service.message_send(send(works[0], 1, works[1]))
    assert denied.status == "denied" and denied.reason == "recipient_route_unavailable"


async def test_stdio_schema_call_and_authenticated_http_vertical(messaging, monkeypatch):
    service, _actor, _principals, works, issued = messaging
    server = build_chatgpt_server(service)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert {"message_send", "message_pending"} <= tools.keys()
    schema = tools["message_send"].input_schema
    assert set(schema["required"]) == {"api_version", "work_id", "grant_version", "message_id", "payload"}
    assert not {"principal", "grant_id", "provider", "projection_target"} & schema["properties"].keys()
    assert (await server.call_tool("message_send", send(works[0], 1, works[1]).model_dump())).structured_content["status"] == "ok"
    config = chatgpt_edge.MCPAuthConfig("client", "secret", "42", "https://switchstand.example/mcp")
    async def verified(_self, token):
        return AccessToken(token=token, client_id="test", scopes=[chatgpt_edge.REQUIRED_SCOPE], subject="42",
            claims={"iss": config.issuer_url}, resource=config.resource_url,
            expires_at=int(time.time()) + 60)
    monkeypatch.setattr(chatgpt_edge.SwitchstandGitHubProvider, "verify_token", verified)
    authenticated = PrincipalContext(issuer=config.issuer_url, subject="42", client_id="test",
                                     assurance="authenticated")
    replacement = grant(principal=authenticated, active=works[0], operations=frozenset({"message"}))
    await service.grants.issue(replacement, None)
    app = chatgpt_edge.create_app(service, config, client_storage=MemoryStore())
    transport = StreamableHttpTransport(config.resource_url, auth="fixed", httpx_client_factory=lambda **kw:
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=config.issuer_url, **kw))
    async with app.router.lifespan_context(app), Client(transport) as client:
        request = send(works[0], 1, works[1]).model_dump(mode="json")
        sent = (await client.call_tool("message_send", request)).structured_content
        assert sent["status"] == "ok"
        replacement = replacement.model_copy(update={"version": 2, "id": uuid4(), "authority": issued[1].authority})
        await service.grants.issue(replacement, 1)
        pending = (await client.call_tool("message_pending", {"api_version": "1",
            "work_id": str(works[1]), "grant_version": 2})).structured_content
        reply = send(works[1], 2, None, route_ref=None, recipient_work_id=None,
                     in_reply_to_delivery_id=sent["message"]["delivery_id"]).model_dump(mode="json")
        assert (await client.call_tool("message_send", reply)).structured_content["status"] == "ok"
        replacement = replacement.model_copy(update={"version": 3, "id": uuid4(), "authority": issued[0].authority})
        await service.grants.issue(replacement, 2)
        returned = (await client.call_tool("message_pending", {"api_version": "1",
            "work_id": str(works[0]), "grant_version": 3})).structured_content
    assert sent["message"] in pending["messages"] and len(returned["messages"]) == 1
