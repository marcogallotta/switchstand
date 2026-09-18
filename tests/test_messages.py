import asyncio
import hashlib
import json
import os
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from chatgpt_fixture import PRINCIPAL, grant
from pydantic import ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.grant_state import GrantState
from switchstand.grants import EffectReceipt, GuardOutcome
from switchstand.messages import (
    DispositionEvidence,
    MessageDispositionRequest,
    MessagePendingRequest,
    MessagePendingResult,
    MessageReceiveRequest,
    MessageRoute,
    MessageState,
    MessageSubmitRequest,
    MessageSubmitResult,
    RuntimeCurrentness,
    message_deliveries,
    message_effect_operation_id,
    message_projection,
    messages,
)


def digest(evidence: DispositionEvidence) -> str:
    value = evidence.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
async def subject():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL message tests")
    assert make_url(url).database == "switchstand_test"
    sync = create_engine(url)
    with sync.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, work_event_handles, lifecycle_obligations, "
            "message_projection, "
            "message_deliveries, messages, effect_intents, work_grants, work_handles CASCADE"
        ))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_async_engine(url)
    grants = GrantState(engine)
    sender, recipient = uuid4(), uuid4()
    sender_principal = PRINCIPAL.model_copy(update={"subject": str(uuid4())})
    recipient_principal = PRINCIPAL.model_copy(update={"subject": str(uuid4())})
    sender_grant = grant(principal=sender_principal, active=sender, reference=uuid4())
    recipient_grant = grant(principal=recipient_principal, active=recipient, reference=uuid4())
    await grants.issue(sender_grant, None)
    await grants.issue(recipient_grant, None)
    yield (MessageState(engine, grants), engine, grants, sender_principal, sender_grant,
           recipient_principal, recipient_grant)
    await engine.dispose()


def request(selected, **changes):
    values = {"api_version": "1", "message_id": uuid4(), "grant_version": selected.version,
              "route_ref": "review", "kind": "request",
              "payload": {"text": "review exact candidate"}}
    return MessageSubmitRequest(**(values | changes))


def route(selected, target="123"):
    return MessageRoute(recipient_work_id=selected.authority.active_work_id,
                        recipient_grant_version=selected.version,
                        projection_provider="asana", projection_target=target)


async def test_route_identity_contract_storage_seam_and_restart(subject):
    state, engine, grants, sender_principal, sender, recipient_principal, recipient = subject
    submitted, selected_route = request(sender), route(recipient)
    first = await state.submit(sender_principal, selected_route, submitted)
    assert first.status == "ok" and first.message.state == "AVAILABLE"
    assert await state.submit(sender_principal, selected_route, submitted) == first
    variants = [
        (selected_route, submitted.model_copy(update={"payload": {"text": "different"}})),
        (selected_route.model_copy(update={"recipient_work_id": uuid4()}), submitted),
        (selected_route.model_copy(update={"recipient_grant_version": 2}), submitted),
        (selected_route.model_copy(update={"projection_target": "456"}), submitted),
    ]
    for selected, candidate in variants:
        assert (await state.submit(sender_principal, selected, candidate)).status == "conflict"
    sender2 = sender.model_copy(update={"id": uuid4(), "version": 2})
    await grants.issue(sender2, 1)
    replay = await state.submit(sender_principal, selected_route,
        submitted.model_copy(update={"grant_version": 2}))
    assert replay == first
    async with engine.begin() as connection:
        await connection.execute(message_deliveries.insert().values(
            delivery_id=uuid4(), sender_work_id=sender.authority.active_work_id,
            message_id=submitted.message_id, recipient_work_id=uuid4(), recipient_grant_version=1,
        ))
        assert len((await connection.execute(select(message_deliveries))).all()) == 2
        projection = (await connection.execute(select(message_projection))).mappings().one()
        assert projection["target"] == "123"
    restarted = MessageState(engine, GrantState(engine))
    pending = await restarted.pending(recipient_principal, MessagePendingRequest(
        api_version="1", grant_version=recipient.version,
    ))
    assert pending.messages == (first.message,)


@pytest.mark.parametrize("model, values", [
    (MessageRoute, {"recipient_work_id": uuid4(), "recipient_grant_version": 1,
                    "projection_provider": "email", "projection_target": "123"}),
    (MessageRoute, {"recipient_work_id": uuid4(), "recipient_grant_version": 1,
                    "projection_provider": "asana", "projection_target": "not-a-gid"}),
    (MessageSubmitRequest, {"api_version": "1", "message_id": uuid4(), "grant_version": 1,
                            "route_ref": "Bad Route", "kind": "request", "payload": {}}),
    (MessageSubmitRequest, {"api_version": "1", "message_id": uuid4(), "grant_version": 1,
                            "route_ref": "review", "kind": "other", "payload": {}}),
    (MessageSubmitRequest, {"api_version": "1", "message_id": uuid4(), "grant_version": 1,
                            "route_ref": "review", "kind": "result", "payload": {}}),
    (MessageSubmitRequest, {"api_version": "1", "message_id": uuid4(), "grant_version": 1,
                            "route_ref": "review", "kind": "request", "payload": {},
                            "in_reply_to_delivery_id": uuid4()}),
    (MessageSubmitRequest, {"api_version": "1", "message_id": uuid4(), "grant_version": 1,
                            "route_ref": "review", "kind": "request", "payload": {},
                            "unexpected": True}),
    (MessagePendingResult, {"status": "ok", "has_more": True}),
    (MessageSubmitResult, {"status": "conflict", "reason": "state_unavailable",
                           "unexpected": True}),
])
def test_protocol_contracts_reject_unapproved_or_impossible_shapes(model, values):
    with pytest.raises(ValidationError):
        model.model_validate(values)


async def test_result_correlation_disposition_replay_and_concurrency(subject):
    state, engine, _, sender_principal, sender, recipient_principal, recipient = subject
    original = await state.submit(sender_principal, route(recipient), request(sender))
    delivery_id = original.message.delivery_id
    runtime = RuntimeCurrentness(generation="run-1", current_generation="run-1")
    receive = MessageReceiveRequest(api_version="1", delivery_id=delivery_id, grant_version=1)
    received = await asyncio.gather(state.receive(recipient_principal, runtime, receive),
                                    state.receive(recipient_principal, runtime, receive))
    assert [value.status for value in received] == ["ok", "ok"]
    reply = request(recipient, kind="result", in_reply_to_delivery_id=delivery_id)
    assert (await state.submit(recipient_principal, route(sender), reply)).status == "ok"
    conflict = await state.submit(recipient_principal, route(sender),
        reply.model_copy(update={"message_id": uuid4()}))
    assert conflict.status == "conflict" and conflict.reason == "reply_identity_conflict"
    evidence = DispositionEvidence(kind="result", result_message_id=reply.message_id)
    disposition = MessageDispositionRequest(api_version="1", delivery_id=delivery_id,
        grant_version=1, disposition_digest=digest(evidence), evidence=evidence)
    first, replay = await asyncio.gather(
        state.disposition(recipient_principal, runtime, disposition),
        state.disposition(recipient_principal, runtime, disposition))
    assert first.status == replay.status == "ok"
    assert first.state == replay.state == "DISPOSITIONED"
    async with engine.connect() as connection:
        assert len((await connection.execute(select(messages))).all()) == 2
        assert len((await connection.execute(select(message_deliveries))).all()) == 2
        assert len((await connection.execute(select(message_projection))).all()) == 2


async def test_restart_currentness_and_unknown_effect_are_fail_closed(subject):
    state, engine, grants, sender_principal, sender, recipient_principal, recipient = subject
    original = await state.submit(sender_principal, route(recipient), request(sender))
    delivery_id = original.message.delivery_id
    receive = MessageReceiveRequest(api_version="1", delivery_id=delivery_id, grant_version=1)
    current = RuntimeCurrentness(generation="run-1", current_generation="run-1")
    unavailable = RuntimeCurrentness(generation="run-1", current_generation=None)
    blocked = await state.receive(recipient_principal, unavailable, receive)
    assert blocked.status == "recovery_required" and blocked.reason == "runtime_currentness_unavailable"
    assert (await state.receive(recipient_principal, current, receive)).status == "ok"
    restarted = MessageState(engine, GrantState(engine))
    pending = await restarted.pending(recipient_principal,
        MessagePendingRequest(api_version="1", grant_version=1))
    assert pending.messages[0].state == "RECEIVED"
    stale = RuntimeCurrentness(generation="run-1", current_generation="run-2")
    assert (await restarted.receive(recipient_principal, stale, receive)).status == "stale"
    operation_id = message_effect_operation_id(delivery_id)
    unknown = GuardOutcome(status="unknown", operation="work_append",
        work_id=recipient.authority.active_work_id, operation_id=operation_id,
        reason="ambiguous_provider_send", effect="unknown", retry="reconcile",
        next_action="Reconcile the recorded effect.")
    await grants.prepare({}, recipient, "fingerprint", unknown)
    evidence = DispositionEvidence(kind="provider_effect", operation_id=operation_id)
    disposition = MessageDispositionRequest(api_version="1", delivery_id=delivery_id,
        grant_version=1, disposition_digest=digest(evidence), evidence=evidence)
    blocked = await restarted.disposition(recipient_principal, current, disposition)
    assert blocked.reason == "effect_evidence_unknown"
    changed_generation = RuntimeCurrentness(generation="run-2", current_generation="run-2")
    blocked = await restarted.disposition(recipient_principal, changed_generation, disposition)
    assert blocked.reason == "receiving_binding_changed"
    replacement = recipient.model_copy(update={"id": uuid4(), "version": 2})
    await grants.issue(replacement, 1)
    assert (await restarted.receive(recipient_principal, current, receive)).status == "stale"
    runtime2 = RuntimeCurrentness(generation="run-2", current_generation="run-2")
    recovery = await restarted.receive(recipient_principal, runtime2,
        receive.model_copy(update={"grant_version": 2}))
    assert recovery.status == "recovery_required" and recovery.reason == "receiving_binding_changed"
    blocked = await restarted.disposition(recipient_principal, current,
        disposition.model_copy(update={"grant_version": 2}))
    assert blocked.reason == "receiving_binding_changed"
    async with engine.connect() as connection:
        row = (await connection.execute(select(message_deliveries).where(
            message_deliveries.c.delivery_id == delivery_id))).mappings().one()
    assert row["state"] == "RECEIVED" and row["receiving_generation"] == "run-1"


async def test_provider_effect_disposition_requires_exact_delivery_correlation(subject):
    state, engine, grants, sender_principal, sender, recipient_principal, recipient = subject
    original = await state.submit(sender_principal, route(recipient), request(sender))
    delivery_id = original.message.delivery_id
    runtime = RuntimeCurrentness(generation="run-1", current_generation="run-1")
    receive = MessageReceiveRequest(api_version="1", delivery_id=delivery_id, grant_version=1)
    assert (await state.receive(recipient_principal, runtime, receive)).status == "ok"

    async def applied_effect(operation_id: UUID) -> None:
        unknown = GuardOutcome(status="unknown", operation="work_append",
            work_id=recipient.authority.active_work_id, operation_id=operation_id,
            reason="send_in_progress", effect="unknown", retry="reconcile",
            next_action="Reconcile the recorded effect.")
        await grants.prepare({}, recipient, f"fingerprint-{operation_id}", unknown)
        await grants.finish(GuardOutcome(status="ok", operation="work_append",
            work_id=recipient.authority.active_work_id, operation_id=operation_id,
            reason="append_confirmed", effect="applied", retry="none",
            next_action="Effect is complete.", receipt=EffectReceipt(
                operation_id=operation_id, principal=recipient_principal,
                grant_id=recipient.id, grant_version=recipient.version,
                work_id=recipient.authority.active_work_id, provider="asana",
                task_gid="123", story_gid="456", text="processed",
                qualification="authoritative_readback")))

    unrelated_id = uuid4()
    await applied_effect(unrelated_id)
    unrelated = DispositionEvidence(kind="provider_effect", operation_id=unrelated_id)
    rejected = await state.disposition(recipient_principal, runtime,
        MessageDispositionRequest(api_version="1", delivery_id=delivery_id, grant_version=1,
            disposition_digest=digest(unrelated), evidence=unrelated))
    assert rejected.status == "recovery_required"
    assert rejected.reason == "effect_evidence_mismatch"
    async with engine.connect() as connection:
        row = (await connection.execute(select(message_deliveries).where(
            message_deliveries.c.delivery_id == delivery_id))).mappings().one()
    assert row["state"] == "RECEIVED"

    exact_id = message_effect_operation_id(delivery_id)
    await applied_effect(exact_id)
    exact = DispositionEvidence(kind="provider_effect", operation_id=exact_id)
    disposition = MessageDispositionRequest(api_version="1", delivery_id=delivery_id,
        grant_version=1, disposition_digest=digest(exact), evidence=exact)
    first = await state.disposition(recipient_principal, runtime, disposition)
    replay = await state.disposition(recipient_principal, runtime, disposition)
    assert first.status == replay.status == "ok"
    assert first.state == replay.state == "DISPOSITIONED"


async def test_current_recipient_grant_resolution_and_ambiguity(subject):
    _state, _engine, grants, _sender_principal, _sender, _recipient_principal, recipient = subject
    resolved = await grants.current_for_active_work(recipient.authority.active_work_id)
    assert resolved == recipient

    alternate_principal = PRINCIPAL.model_copy(update={"subject": str(uuid4())})
    alternate = grant(
        principal=alternate_principal,
        active=recipient.authority.active_work_id,
        reference=uuid4(),
    )
    await grants.issue(alternate, None)
    with pytest.raises(ValueError, match="multiple current grants own the same active work"):
        await grants.current_for_active_work(recipient.authority.active_work_id)


async def test_reply_context_and_authorized_replacement_rebind_same_delivery(subject):
    state, _engine, grants, sender_principal, sender, recipient_principal, recipient = subject
    submitted = await state.submit(sender_principal, route(recipient), request(sender))
    assert submitted.status == "ok" and submitted.message is not None
    delivery_id = submitted.message.delivery_id

    context = await state.reply_context(delivery_id)
    assert context is not None
    assert context.recipient_work_id == sender.authority.active_work_id
    assert context.route_ref == "review"

    run1 = RuntimeCurrentness(generation="run-1", current_generation="run-1")
    receive = MessageReceiveRequest(api_version="1", delivery_id=delivery_id, grant_version=1)
    first = await state.receive(recipient_principal, run1, receive)
    assert first.status == "ok" and first.state == "RECEIVED"

    replacement = recipient.model_copy(update={"id": uuid4(), "version": 2})
    await grants.issue(replacement, 1)
    run2 = RuntimeCurrentness(generation="run-2", current_generation="run-2")

    blocked = await state.receive(
        recipient_principal, run2, receive.model_copy(update={"grant_version": 2})
    )
    assert blocked.status == "recovery_required"
    assert blocked.reason == "receiving_binding_changed"

    recovered = await state.recover(
        recipient_principal, run2, receive.model_copy(update={"grant_version": 2})
    )
    assert recovered.status == "ok" and recovered.state == "RECEIVED"

    replay = await state.receive(
        recipient_principal, run2, receive.model_copy(update={"grant_version": 2})
    )
    assert replay == recovered

    stale_old = await state.receive(recipient_principal, run1, receive)
    assert stale_old.status == "stale"
    assert stale_old.reason == "grant_version_changed"
