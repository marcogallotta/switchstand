import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.messages import (
    MessageRoute,
    MessageState,
    MessageSubmitRequest,
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
    assert projection["state"] == "PENDING" and projection["operation_id"] is not None

    restarted = MessageState(engine)
    pending = await restarted.pending(recipient, None, 10)
    assert pending.status == "ok" and pending.messages == (first.message,)
    independently_received = await restarted.pending(another, None, 10)
    assert independently_received.messages[0].state == "RECEIVED"
