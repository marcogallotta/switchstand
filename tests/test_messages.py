import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from chatgpt_fixture import PRINCIPAL, grant
from pydantic import ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.grant_state import GrantState
from switchstand.messages import (
    MessagePendingRequest,
    MessagePendingResult,
    MessageRoute,
    MessageState,
    MessageSubmitRequest,
    MessageSubmitResult,
    message_deliveries,
    message_projection,
)


@pytest.fixture
async def subject():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL message tests")
    assert make_url(url).database == "switchstand_test"
    sync = create_engine(url)
    with sync.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, message_projection, message_deliveries, "
            "messages, effect_intents, work_grants, work_handles CASCADE"
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
