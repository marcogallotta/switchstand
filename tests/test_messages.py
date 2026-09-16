import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.messages import (
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
    yield MessageState(engine), engine
    await engine.dispose()


async def test_real_postgres_message_identity_delivery_seam_restart_and_projection(subject):
    state, engine = subject
    sender, recipient, another = uuid4(), uuid4(), uuid4()
    route = MessageRoute(
        recipient_work_id=recipient, projection_provider="asana", projection_target="123"
    )
    request = MessageSubmitRequest(
        api_version="1", message_id=uuid4(), route_ref="review", kind="request",
        payload={"text": "review exact candidate"},
    )

    first = await state.submit(sender, 4, route, request)
    assert first.status == "ok" and first.message.state == "AVAILABLE"
    assert await state.submit(sender, 4, route, request) == first
    conflict = await state.submit(sender, 4, route, request.model_copy(update={
        "payload": {"text": "different"}
    }))
    assert conflict.status == "conflict" and conflict.reason == "message_identity_conflict"
    for changed in ({"recipient_work_id": another}, {"projection_target": "456"}):
        rerouted = await state.submit(sender, 4, route.model_copy(update=changed), request)
        assert rerouted.status == "conflict"

    second_delivery = uuid4()
    async with engine.begin() as connection:
        await connection.execute(message_deliveries.insert().values(
            delivery_id=second_delivery, sender_work_id=sender, message_id=request.message_id,
            recipient_work_id=another, recipient_grant_version=1,
        ))
        await connection.execute(update(message_deliveries).where(
            message_deliveries.c.delivery_id == second_delivery
        ).values(state="RECEIVED"))
        projection = (await connection.execute(select(message_projection))).mappings().one()
        deliveries = (await connection.execute(select(message_deliveries))).mappings().all()
    assert projection["state"] == "PENDING" and projection["operation_id"] is not None
    assert len(deliveries) == 2

    restarted = MessageState(engine)
    pending = await restarted.pending(recipient, None, 10)
    assert pending.status == "ok" and pending.messages == (first.message,)
    independently_received = await restarted.pending(another, None, 10)
    assert independently_received.messages[0].state == "RECEIVED"
    result = MessageSubmitRequest(api_version="1", message_id=uuid4(), route_ref="review",
        kind="result", payload={"text": "done"}, in_reply_to_delivery_id=first.message.delivery_id)
    reply_route = MessageRoute(recipient_work_id=sender, projection_provider="asana",
                               projection_target="789")
    assert (await state.submit(recipient, 4, reply_route, result)).status == "ok"
    duplicate = await state.submit(recipient, 4, reply_route,
        result.model_copy(update={"message_id": uuid4()}))
    assert duplicate.reason == "reply_identity_conflict"
    async with engine.connect() as connection:
        assert len((await connection.execute(select(message_projection))).all()) == 2
        assert len((await connection.execute(select(message_deliveries))).all()) == 3


@pytest.mark.parametrize("values", [
    {"recipient_work_id": uuid4(), "projection_provider": "email", "projection_target": "123"},
    {"recipient_work_id": uuid4(), "projection_provider": "asana",
     "projection_target": "not-a-gid", "unexpected": True},
])
def test_route_contract_is_closed(values):
    with pytest.raises(ValidationError):
        MessageRoute.model_validate(values)


@pytest.mark.parametrize("model, values", [
    (MessageSubmitRequest, {"api_version": "1", "message_id": uuid4(),
        "route_ref": "Bad Route", "kind": "request", "payload": {}}),
    (MessageSubmitRequest, {"api_version": "1", "message_id": uuid4(),
        "route_ref": "review", "kind": "other", "payload": {}}),
    (MessageSubmitRequest, {"api_version": "1", "message_id": uuid4(),
        "route_ref": "review", "kind": "request", "payload": {}, "unexpected": True}),
    (MessageSubmitRequest, {"api_version": "1", "message_id": uuid4(),
        "route_ref": "review", "kind": "result", "payload": {}}),
    (MessageSubmitRequest, {"api_version": "1", "message_id": uuid4(),
        "route_ref": "review", "kind": "request", "payload": {},
        "in_reply_to_delivery_id": uuid4()}),
    (MessageSubmitResult, {"status": "ok"}),
    (MessagePendingResult, {"status": "ok", "has_more": True}),
])
def test_request_and_result_contracts_reject_impossible_shapes(model, values):
    with pytest.raises(ValidationError):
        model.model_validate(values)
