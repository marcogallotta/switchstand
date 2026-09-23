import asyncio
import hashlib
import json
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from mcp import Client
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.contracts import LaunchAuthority
from switchstand.core import Controller
from switchstand.grant_state import GrantState
from switchstand.grants import EffectReceipt, GuardOutcome
from switchstand.managed_identity import managed_principal, rotate_managed_grant
from switchstand.mcp import build_server
from switchstand.messages import (
    DispositionEvidence,
    MessageRoute,
    MessageState,
    MessageSubmitRequest,
    RuntimeCurrentness,
    message_deliveries,
    message_effect_operation_id,
    message_projection,
)
from switchstand.messages import messages as message_rows
from switchstand.state import PostgresState


async def test_managed_mcp_replacement_result_and_disposition_vertical(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    assert make_url(url).database == "switchstand_test"
    sync = create_engine(url)
    with sync.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, work_event_handles, lifecycle_obligations, "
            "message_projection, message_deliveries, messages, effect_intents, "
            "work_grants, work_handles CASCADE"
        ))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_async_engine(url)
    try:
        state, grants = PostgresState(engine), GrantState(engine)
        sender = await state.bind("asana", "111")
        recipient = await state.bind("asana", "222")
        sender_authority = LaunchAuthority(active_work_id=sender.id)
        sender_grant = await rotate_managed_grant(grants, sender_authority)
        recipient_authority = LaunchAuthority(active_work_id=recipient.id)
        recipient_grant = await rotate_managed_grant(grants, recipient_authority)
        messages = MessageState(engine, grants)
        request_id = uuid4()
        submitted = await messages.submit(
            managed_principal(sender.id),
            MessageRoute(
                recipient_work_id=recipient.id,
                recipient_grant_version=recipient_grant.version,
                projection_provider="asana", projection_target="222",
            ),
            MessageSubmitRequest(
                api_version="1", message_id=request_id,
                grant_version=sender_grant.version, route_ref="review", kind="request",
                payload={"question": "review"},
            ),
        )
        assert submitted.message is not None
        delivery_id = submitted.message.delivery_id
        runtime = [RuntimeCurrentness(generation="run-1", current_generation="run-1")]
        server = build_server(
            Controller(recipient_authority, state, {}), recipient.id,
            messages=messages, grants=grants, principal=managed_principal(recipient.id),
            currentness=lambda: runtime[0],
        )
        async with Client(server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert all(
                tools[name].input_schema.get("additionalProperties") is False
                for name in tools if name.startswith("message_")
            )
            base = {"api_version": "1"}
            pending = await client.call_tool("message_pending", base)
            assert pending.structured_content["messages"][0]["delivery_id"] == str(delivery_id)
            before_receive = await client.call_tool("message_result_send", {
                "api_version": "1", "in_reply_to_delivery_id": str(delivery_id),
                "message_id": str(uuid4()), "payload": {"answer": "too early"},
            })
            assert before_receive.structured_content["status"] == "conflict"
            received = await client.call_tool(
                "message_receive", base | {"delivery_id": str(delivery_id)}
            )
            assert received.structured_content["state"] == "RECEIVED"

            replacement_grant = await rotate_managed_grant(grants, recipient_authority)
            runtime[0] = RuntimeCurrentness(
                generation="run-1", current_generation="run-2"
            )
            assert (await client.call_tool("message_pending", {
                "api_version": "1",
            })).structured_content["status"] == "recovery_required"
            runtime[0] = RuntimeCurrentness(generation="run-2", current_generation="run-2")
            before_recover = await client.call_tool("message_result_send", {
                "api_version": "1", "in_reply_to_delivery_id": str(delivery_id),
                "message_id": str(uuid4()), "payload": {"answer": "not recovered"},
            })
            assert before_recover.structured_content["status"] == "conflict"
            recovered = await client.call_tool("message_recover", {
                "api_version": "1", "delivery_id": str(delivery_id),
            })
            assert recovered.structured_content["state"] == "RECEIVED"

            raced = await messages.submit(
                managed_principal(sender.id),
                MessageRoute(
                    recipient_work_id=recipient.id,
                    recipient_grant_version=2,
                    projection_provider="asana", projection_target="222",
                ),
                MessageSubmitRequest(
                    api_version="1", message_id=uuid4(), grant_version=sender_grant.version,
                    route_ref="review", kind="request", payload={"question": "race"},
                ),
            )
            assert raced.message is not None
            race_delivery_id = raced.message.delivery_id
            assert (await client.call_tool("message_receive", {
                "api_version": "1", "delivery_id": str(race_delivery_id),
            })).structured_content["state"] == "RECEIVED"
            operation_id = message_effect_operation_id(race_delivery_id)
            await grants.prepare({}, replacement_grant, "race-evidence", GuardOutcome(
                status="unknown", operation="work_append", work_id=recipient.id,
                operation_id=operation_id, reason="send_in_progress", effect="unknown",
                retry="reconcile", next_action="Reconcile the recorded effect.",
            ))
            await grants.finish(GuardOutcome(
                status="ok", operation="work_append", work_id=recipient.id,
                operation_id=operation_id, reason="append_confirmed", effect="applied",
                retry="none", next_action="Effect is complete.", receipt=EffectReceipt(
                    operation_id=operation_id, principal=managed_principal(recipient.id),
                    grant_id=replacement_grant.id, grant_version=replacement_grant.version,
                    work_id=recipient.id, provider="asana", task_gid="222",
                    story_gid="333", text="processed",
                    qualification="authoritative_readback",
                ),
            ))
            race_result_id = uuid4()
            evidence = DispositionEvidence(kind="provider_effect", operation_id=operation_id)
            encoded = evidence.model_dump(mode="json")
            disposition_digest = hashlib.sha256(json.dumps(
                encoded, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest()
            async with engine.connect() as disposition_connection:
                transaction = await disposition_connection.begin()
                await disposition_connection.execute(update(message_deliveries).where(
                    message_deliveries.c.delivery_id == race_delivery_id
                ).values(
                    state="DISPOSITIONED", dispositioned_at=func.now(),
                    disposition_digest=disposition_digest, evidence=encoded,
                ))
                creating = asyncio.create_task(client.call_tool("message_result_send", {
                    "api_version": "1",
                    "in_reply_to_delivery_id": str(race_delivery_id),
                    "message_id": str(race_result_id), "payload": {"answer": "too late"},
                }))
                done, _ = await asyncio.wait({creating}, timeout=0.1)
                assert not done
                await transaction.commit()
            assert (await creating).structured_content["status"] == "conflict"
            async with engine.connect() as connection:
                assert await connection.scalar(select(func.count()).select_from(
                    message_rows
                ).where(message_rows.c.message_id == race_result_id)) == 0
                assert await connection.scalar(select(func.count()).select_from(
                    message_projection
                ).where(message_projection.c.message_id == race_result_id)) == 0

            result_id = uuid4()
            result = await client.call_tool("message_result_send", {
                "api_version": "1",
                "in_reply_to_delivery_id": str(delivery_id),
                "message_id": str(result_id), "payload": {"answer": "pass"},
            })
            assert result.structured_content["message"]["kind"] == "result"
            disposition = await client.call_tool("message_disposition", {
                "api_version": "1",
                "delivery_id": str(delivery_id), "result_message_id": str(result_id),
            })
            assert disposition.structured_content["state"] == "DISPOSITIONED"
            replay = await client.call_tool("message_result_send", {
                "api_version": "1",
                "in_reply_to_delivery_id": str(delivery_id),
                "message_id": str(result_id), "payload": {"answer": "pass"},
            })
            assert replay.structured_content == result.structured_content
            assert (await client.call_tool("message_disposition", {
                "api_version": "1",
                "delivery_id": str(delivery_id), "result_message_id": str(result_id),
            })).structured_content == disposition.structured_content
            monkeypatch.setattr(grants, "current", AsyncMock(side_effect=SQLAlchemyError()))
            unavailable = await client.call_tool("message_pending", {"api_version": "1"})
            assert unavailable.structured_content["reason"] == "state_unavailable"
    finally:
        await engine.dispose()
