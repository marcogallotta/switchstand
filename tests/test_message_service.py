import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.contracts import LaunchAuthority
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext, WorkGrant
from switchstand.message_service import (
    MessageDelivery,
    MessageDisposition,
    MessagePending,
    MessageReply,
    MessageSend,
    MessageService,
)
from switchstand.messages import MessageState, message_projection
from switchstand.state import PostgresState, metadata


@pytest.fixture
async def engine() -> AsyncEngine:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for semantic messaging tests")
    if make_url(url).database != "switchstand_test":
        pytest.fail("semantic messaging tests require switchstand_test")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.run_sync(metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()


def principal(name: str) -> PrincipalContext:
    return PrincipalContext(
        issuer="message-service-test",
        subject=name,
        client_id="local-test",
        assurance="test",
    )


def grant(who: PrincipalContext, work_id, version: int = 1) -> WorkGrant:
    return WorkGrant(
        id=uuid4(),
        version=version,
        principal=who,
        authority=LaunchAuthority(active_work_id=work_id),
        operations=frozenset({"work_get", "message", "work_append"}),
        issuer="fixture-operator",
        provenance="semantic message service",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        append_qualification="test:message-service",
    )


async def service(
    who: PrincipalContext,
    state: PostgresState,
    grants: GrantState,
    messages: MessageState,
    generation: str,
    current: dict[str, str | None] | None = None,
) -> MessageService:
    async def resolve():
        return who

    owned = {"value": generation} if current is None else current

    async def current_generation():
        return owned["value"]

    return MessageService(
        resolve, state, grants, messages, generation, current_generation
    )


async def setup(engine: AsyncEngine):
    state = PostgresState(engine)
    grants = GrantState(engine)
    messages = MessageState(engine, grants)
    sender_work = await state.bind("asana", "101")
    recipient_work = await state.bind("asana", "202")
    sender, recipient = principal("sender"), principal("recipient")
    sender_grant, recipient_grant = grant(sender, sender_work.id), grant(recipient, recipient_work.id)
    await grants.issue(sender_grant, None)
    await grants.issue(recipient_grant, None)
    return (
        state, grants, messages,
        sender, sender_grant, sender_work,
        recipient, recipient_grant, recipient_work,
    )


async def test_semantic_send_reply_disposition_hides_provider_route(engine):
    (
        state, grants, messages,
        sender, _sender_grant, sender_work,
        recipient, _recipient_grant, recipient_work,
    ) = await setup(engine)
    sender_service = await service(sender, state, grants, messages, "chatgpt-1")
    recipient_service = await service(recipient, state, grants, messages, "codex-1")

    message_id = uuid4()
    first = await sender_service.send(MessageSend(
        api_version="1",
        message_id=message_id,
        recipient_work_id=recipient_work.id,
        route_ref="review",
        payload={"text": "review exact candidate"},
    ))
    assert first.status == "ok" and first.message is not None
    delivery_id = first.message.delivery_id
    assert first.message.sender_work_id == sender_work.id
    assert first.message.recipient_work_id == recipient_work.id

    replay = await sender_service.send(MessageSend(
        api_version="1",
        message_id=message_id,
        recipient_work_id=recipient_work.id,
        route_ref="review",
        payload={"text": "review exact candidate"},
    ))
    assert replay == first

    async with engine.connect() as connection:
        projection = (await connection.execute(select(message_projection))).mappings().one()
    assert projection["provider"] == "asana"
    assert projection["target"] == "202"

    pending = await recipient_service.pending(MessagePending(api_version="1"))
    assert pending.status == "ok" and pending.messages == (first.message,)

    received = await recipient_service.receive(MessageDelivery(
        api_version="1", delivery_id=delivery_id
    ))
    assert received.status == "ok" and received.state == "RECEIVED"

    result_id = uuid4()
    reply = await recipient_service.reply(MessageReply(
        api_version="1",
        message_id=result_id,
        in_reply_to_delivery_id=delivery_id,
        payload={"text": "PASS"},
    ))
    assert reply.status == "ok" and reply.message is not None
    assert reply.message.recipient_work_id == sender_work.id
    assert reply.message.route_ref == "review"

    disposed = await recipient_service.disposition(MessageDisposition(
        api_version="1",
        delivery_id=delivery_id,
        result_message_id=result_id,
    ))
    assert disposed.status == "ok" and disposed.state == "DISPOSITIONED"

    sender_pending = await sender_service.pending(MessagePending(api_version="1"))
    assert sender_pending.status == "ok"
    assert [item.message_id for item in sender_pending.messages] == [result_id]


async def test_workspace_actor_is_explicit_and_reply_routes_to_same_work(engine):
    state = PostgresState(engine)
    grants = GrantState(engine)
    messages = MessageState(engine, grants)
    sender_work = await state.bind("asana", "101")
    workspace_anchor = await state.bind("asana", "102")
    recipient_work = await state.bind("asana", "202")
    sender, recipient = principal("workspace-sender"), principal("recipient")
    sender_grant = WorkGrant(
        id=uuid4(),
        version=1,
        principal=sender,
        authority=LaunchAuthority(active_work_id=workspace_anchor.id),
        scope="workspace",
        operations=frozenset({"work_get", "message"}),
        issuer="fixture-operator",
        provenance="workspace semantic message service",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    recipient_grant = grant(recipient, recipient_work.id)
    await grants.issue(sender_grant, None)
    await grants.issue(recipient_grant, None)
    sender_service = await service(sender, state, grants, messages, "workspace")
    recipient_service = await service(recipient, state, grants, messages, "codex-1")

    missing = await sender_service.send(MessageSend(
        api_version="1",
        message_id=uuid4(),
        recipient_work_id=recipient_work.id,
        route_ref="review",
        payload={"text": "missing actor"},
    ))
    assert missing.status == "denied" and missing.reason == "explicit_work_id_required"

    submitted = await sender_service.send(MessageSend(
        api_version="1",
        work_id=sender_work.id,
        message_id=uuid4(),
        recipient_work_id=recipient_work.id,
        route_ref="review",
        payload={"text": "review candidate"},
    ))
    assert submitted.status == "ok" and submitted.message is not None
    assert submitted.message.sender_work_id == sender_work.id
    assert submitted.message.sender_work_id != workspace_anchor.id

    delivery = MessageDelivery(
        api_version="1", delivery_id=submitted.message.delivery_id
    )
    received = await recipient_service.receive(delivery)
    assert received.status == "ok"

    reply_id = uuid4()
    reply = await recipient_service.reply(MessageReply(
        api_version="1",
        message_id=reply_id,
        in_reply_to_delivery_id=delivery.delivery_id,
        payload={"text": "PASS"},
    ))
    assert reply.status == "ok" and reply.message is not None
    assert reply.message.recipient_work_id == sender_work.id

    pending = await sender_service.pending(MessagePending(
        api_version="1", work_id=sender_work.id
    ))
    assert pending.status == "ok"
    assert [item.message_id for item in pending.messages] == [reply_id]

    wrong = await sender_service.pending(MessagePending(
        api_version="1", work_id=uuid4()
    ))
    assert wrong.status == "denied" and wrong.reason == "work_not_granted"


async def test_workspace_messaging_requires_explicit_capability(engine):
    state = PostgresState(engine)
    grants = GrantState(engine)
    messages = MessageState(engine, grants)
    actor_work = await state.bind("asana", "101")
    recipient_work = await state.bind("asana", "202")
    who = principal("workspace-no-message")
    selected = WorkGrant(
        id=uuid4(), version=1, principal=who,
        authority=LaunchAuthority(active_work_id=actor_work.id),
        scope="workspace",
        operations=frozenset({"work_get"}),
        issuer="fixture-operator", provenance="no message capability",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await grants.issue(selected, None)
    subject = await service(who, state, grants, messages, "workspace")
    denied = await subject.send(MessageSend(
        api_version="1", work_id=actor_work.id, message_id=uuid4(),
        recipient_work_id=recipient_work.id, route_ref="review", payload={},
    ))
    assert denied.status == "denied" and denied.reason == "work_not_granted"


async def test_workspace_route_ambiguity_fails_closed(engine):
    state = PostgresState(engine)
    grants = GrantState(engine)
    messages = MessageState(engine, grants)
    sender_work = await state.bind("asana", "101")
    recipient_work = await state.bind("asana", "303")
    sender = principal("sender")
    await grants.issue(grant(sender, sender_work.id), None)
    for index in range(2):
        anchor = await state.bind("asana", f"40{index}")
        who = principal(f"workspace-{index}")
        selected = WorkGrant(
            id=uuid4(), version=1, principal=who,
            authority=LaunchAuthority(active_work_id=anchor.id),
            scope="workspace",
            operations=frozenset({"work_get", "message"}),
            issuer="fixture-operator", provenance="ambiguous workspace route",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await grants.issue(selected, None)

    subject = await service(sender, state, grants, messages, "sender-run")
    blocked = await subject.send(MessageSend(
        api_version="1", message_id=uuid4(), recipient_work_id=recipient_work.id,
        route_ref="review", payload={"text": "must fail closed"},
    ))
    assert blocked.status == "recovery_required"
    assert blocked.reason == "state_unavailable"


async def test_missing_authoritative_generation_fails_closed(engine):
    (
        state, grants, messages,
        sender, _sender_grant, _sender_work,
        _recipient, _recipient_grant, recipient_work,
    ) = await setup(engine)

    async def resolve():
        return sender

    subject = MessageService(
        resolve, state, grants, messages, "chatgpt-1", current_generation=None
    )
    blocked_send = await subject.send(MessageSend(
        api_version="1",
        message_id=uuid4(),
        recipient_work_id=recipient_work.id,
        route_ref="review",
        payload={"text": "must not self-attest"},
    ))
    assert blocked_send.status == "recovery_required"
    assert blocked_send.reason == "runtime_currentness_unavailable"

    blocked_pending = await subject.pending(MessagePending(api_version="1"))
    assert blocked_pending.status == "recovery_required"
    assert blocked_pending.reason == "runtime_currentness_unavailable"


async def test_replacement_recovers_same_received_delivery_identity(engine):
    (
        state, grants, messages,
        sender, _sender_grant, sender_work,
        recipient, recipient_grant, recipient_work,
    ) = await setup(engine)
    sender_service = await service(sender, state, grants, messages, "chatgpt-1")
    recipient_generation = {"value": "run-1"}
    old_recipient = await service(
        recipient, state, grants, messages, "run-1", recipient_generation
    )

    submitted = await sender_service.send(MessageSend(
        api_version="1",
        message_id=uuid4(),
        recipient_work_id=recipient_work.id,
        route_ref="handoff",
        payload={"text": "continue"},
    ))
    assert submitted.status == "ok" and submitted.message is not None
    delivery = MessageDelivery(api_version="1", delivery_id=submitted.message.delivery_id)
    first = await old_recipient.receive(delivery)
    assert first.status == "ok" and first.state == "RECEIVED"

    replacement_grant = grant(recipient, recipient_work.id, version=2)
    await grants.issue(replacement_grant, recipient_grant.version)
    recipient_generation["value"] = "run-2"
    replacement = await service(
        recipient, state, grants, messages, "run-2", recipient_generation
    )

    blocked = await replacement.receive(delivery)
    assert blocked.status == "recovery_required"
    assert blocked.reason == "receiving_binding_changed"

    recovered = await replacement.recover(delivery)
    assert recovered.status == "ok" and recovered.state == "RECEIVED"
    assert await replacement.receive(delivery) == recovered

    stale_receive = await old_recipient.receive(delivery)
    assert stale_receive.status == "stale"
    assert stale_receive.reason == "runtime_generation_changed"

    stale_recover = await old_recipient.recover(delivery)
    assert stale_recover.status == "stale"
    assert stale_recover.reason == "runtime_generation_changed"

    stale_pending = await old_recipient.pending(MessagePending(api_version="1"))
    assert stale_pending.status == "stale"
    assert stale_pending.reason == "runtime_generation_changed"

    stale_send = await old_recipient.send(MessageSend(
        api_version="1",
        message_id=uuid4(),
        recipient_work_id=sender_work.id,
        route_ref="handoff",
        payload={"text": "stale send"},
    ))
    assert stale_send.status == "stale"
    assert stale_send.reason == "runtime_generation_changed"

    stale_reply = await old_recipient.reply(MessageReply(
        api_version="1",
        message_id=uuid4(),
        in_reply_to_delivery_id=delivery.delivery_id,
        payload={"text": "stale reply"},
    ))
    assert stale_reply.status == "stale"
    assert stale_reply.reason == "runtime_generation_changed"

    stale_disposition = await old_recipient.disposition(MessageDisposition(
        api_version="1",
        delivery_id=delivery.delivery_id,
        result_message_id=uuid4(),
    ))
    assert stale_disposition.status == "stale"
    assert stale_disposition.reason == "runtime_generation_changed"


async def test_missing_recipient_route_fails_without_provider_identity(engine):
    (
        state, grants, messages,
        sender, _sender_grant, _sender_work,
        _recipient, _recipient_grant, _recipient_work,
    ) = await setup(engine)
    sender_service = await service(sender, state, grants, messages, "chatgpt-1")
    unowned = await state.bind("asana", "303")
    result = await sender_service.send(MessageSend(
        api_version="1",
        message_id=uuid4(),
        recipient_work_id=unowned.id,
        route_ref="review",
        payload={"text": "should not route"},
    ))
    assert result.status == "recovery_required"
    assert result.reason == "recipient_route_unavailable"


def test_semantic_disposition_requires_one_exact_evidence_identity():
    with pytest.raises(ValueError):
        MessageDisposition(api_version="1", delivery_id=uuid4())
    with pytest.raises(ValueError):
        MessageDisposition(
            api_version="1",
            delivery_id=uuid4(),
            result_message_id=uuid4(),
            operation_id=uuid4(),
        )
