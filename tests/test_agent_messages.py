import asyncio
import os
from typing import Literal
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.agent_mailboxes import (
    CODEX_COORDINATOR_KEY,
    CODEX_COORDINATOR_NAME,
    AgentMailboxResult,
    AgentMailboxState,
    agent_mailboxes,
)
from switchstand.agent_messages import (
    AgentMessagePendingResult,
    AgentMessageSubmitResult,
    AgentPendingMessage,
)
from switchstand.chatgpt import ChatGPTService
from switchstand.chatgpt_edge import _codex_stop_mailbox_page
from switchstand.chatgpt_mcp import build_ordinary_tools
from switchstand.contracts import ClosedModel
from switchstand.durable_capture import DurableCaptureGuidance
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext
from switchstand.messages import MessageState, message_deliveries, messages
from switchstand.state import PostgresState, metadata


class StrictPreDeployPendingItem(ClosedModel):
    delivery_id: UUID
    message_id: UUID
    sender_name: str
    recipient_name: str
    kind: Literal["request", "result"]
    payload: JsonValue
    state: Literal["AVAILABLE", "RECEIVED", "DISPOSITIONED"]
    durable_capture: DurableCaptureGuidance


class StrictPreDeployPendingResult(ClosedModel):
    status: Literal["ok", "denied", "stale", "recovery_required"]
    messages: tuple[StrictPreDeployPendingItem, ...]
    next_cursor: UUID | None
    has_more: bool
    reason: str | None
    next_action: str | None


class StrictPreDeploySubmitResult(ClosedModel):
    status: Literal["ok", "conflict", "denied", "stale", "recovery_required"]
    message: StrictPreDeployPendingItem | None
    reason: str | None
    next_action: str | None


def test_api_v1_mailbox_outputs_remain_compatible_with_strict_predeploy_clients():
    current_item_schema = AgentPendingMessage.model_json_schema()
    prior_item_schema = StrictPreDeployPendingItem.model_json_schema()
    assert current_item_schema["additionalProperties"] is False
    assert current_item_schema["properties"] == prior_item_schema["properties"]

    message = AgentPendingMessage(
        delivery_id=uuid4(), message_id=uuid4(), sender_name="Alpha",
        recipient_name="Beta", kind="request", payload={"request": "review"},
        state="RECEIVED",
    )
    StrictPreDeployPendingResult.model_validate(AgentMessagePendingResult(
        status="ok", messages=(message,),
    ).model_dump(mode="json"))
    StrictPreDeploySubmitResult.model_validate(AgentMessageSubmitResult(
        status="ok", message=message,
    ).model_dump(mode="json"))


@pytest.fixture
async def agent_messaging(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, activation_obligation_revisions"
        ))
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
        agent_identity=lambda: session[0],
    ))
    yield tools, actor, session, owner, other, service
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, activation_obligation_revisions"
        ))
    await engine.dispose()


async def bind_root(tools, service) -> None:
    assert (await tools["agent_register"]("1", "Coordinator")).status == "ok"
    assert service.messages is not None
    async with service.messages.engine.begin() as connection:
        await connection.execute(update(agent_mailboxes).where(
            agent_mailboxes.c.name_key == "coordinator"
        ).values(name_key="root", display_name="/root"))


def root_tools(service, session, *, audit=None):
    return dict(build_ordinary_tools(
        service,
        audit=audit,
        agent_identity=lambda: session[0],
    ))


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


async def test_registration_replay_names_the_existing_identity(agent_messaging):
    tools, _actor, _session, _owner, _other, _service = agent_messaging
    first = await tools["agent_register"]("1", "Alpha")
    replay = await tools["agent_register"]("1", "Beta")

    assert first.status == "ok" and first.name == "Alpha" and first.next_action is None
    assert replay.status == "ok" and replay.name == "Alpha" and replay.reason is None
    assert replay.next_action is not None
    assert "remains registered as 'Alpha'" in replay.next_action
    assert "requested name 'Beta' was not registered" in replay.next_action
    assert "Do not treat this result as registration under 'Beta'" in replay.next_action


async def test_registration_replay_accepts_equivalent_name(agent_messaging):
    tools, _actor, _session, _owner, _other, _service = agent_messaging
    assert (await tools["agent_register"]("1", "Alpha Agent")).status == "ok"

    replay = await tools["agent_register"]("1", "  ALPHA   agent  ")

    assert replay.status == "ok" and replay.name == "Alpha Agent"
    assert replay.next_action is None


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
    assert "pending result" in completed_replay.next_action
    assert "bounded wait" not in completed_replay.next_action
    pending = await tools["agent_message_pending"]("1")
    assert [item.message_id for item in pending.messages] == [result_id]


async def test_missing_runtime_identity_is_local_to_agent_messaging(agent_messaging):
    tools, _actor, session, _owner, _other, service = agent_messaging
    session[0] = ""
    registration = await tools["agent_register"]("1", "Alpha")
    assert (registration.status, registration.reason) == (
        "recovery_required", "runtime_identity_unavailable",
    )
    # Unrelated ordinary tools remain registered and callable through their own admission paths.
    assert "work_get" in tools and "repository_bundle_get" in tools

    legacy_only = dict(build_ordinary_tools(service))
    unavailable = await legacy_only["agent_register"]("1", "TransportFallback")
    assert (unavailable.status, unavailable.reason) == (
        "recovery_required", "runtime_identity_unavailable",
    )


async def test_unregistered_send_and_pending_direct_actor_to_register(agent_messaging):
    tools, _actor, session, _owner, _other, _service = agent_messaging
    session[0] = "recipient-chat"
    assert (await tools["agent_register"]("1", "Recipient")).status == "ok"
    session[0] = "unregistered-chat"

    sent = await tools["agent_message_send"](
        "1", "Recipient", uuid4(), {"request": "hello"},
    )
    pending = await tools["agent_message_pending"]("1")

    for result in (sent, pending):
        assert (result.status, result.reason) == ("denied", "agent_not_registered")
        assert result.next_action is not None
        assert "agent_register" in result.next_action
        assert "intended durable agent name" in result.next_action
        assert "Do not infer Codex Coordinator" in result.next_action

    session[0] = "recipient-chat"
    missing = await tools["agent_message_send"](
        "1", "Missing Recipient", uuid4(), {"request": "hello"},
    )
    assert (missing.status, missing.reason) == ("denied", "recipient_not_registered")
    assert missing.next_action is None


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
    assert (denied.status, denied.reason) == (
        "denied", "takeover_confirmation_required",
    )
    assert denied.next_action is not None

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


async def test_marco_authorized_named_takeover_crosses_principal_and_keeps_mail(
    agent_messaging,
):
    tools, actor, session, owner, other, service = agent_messaging
    assert (await tools["agent_register"]("1", "Tests")).status == "ok"
    assert service.messages is not None
    before = (await AgentMailboxState(service.messages.engine).by_name("Tests")).mailbox
    assert before is not None
    session[0] = "sender-chat"
    assert (await tools["agent_register"]("1", "Sender")).status == "ok"
    sent = await tools["agent_message_send"](
        "1", "Tests", uuid4(), {"request": "kept"},
    )
    assert sent.status == "ok" and sent.message is not None

    actor[0], session[0] = other, "replacement-chat"
    configured = root_tools(service, session)
    moved = await configured["agent_takeover"](
        "1", "Tests", "Marco says: take over Tests.",
    )

    assert (moved.status, moved.name) == ("ok", "Tests")
    current = (await AgentMailboxState(service.messages.engine).by_name("Tests")).mailbox
    assert current is not None
    assert current.endpoint_id == before.endpoint_id
    assert current.generation == before.generation + 1
    pending = await configured["agent_message_pending"]("1")
    assert [item.delivery_id for item in pending.messages] == [sent.message.delivery_id]

    actor[0], session[0] = owner, "chat-a"
    fenced = await configured["agent_message_pending"]("1")
    assert (fenced.status, fenced.reason) == ("denied", "agent_not_registered")


async def test_named_cross_principal_takeover_rejects_missing_marco_authorization(
    agent_messaging,
):
    tools, actor, session, _owner, other, service = agent_messaging
    assert (await tools["agent_register"]("1", "Tests")).status == "ok"
    actor[0], session[0] = other, "replacement-chat"
    configured = root_tools(service, session)

    missing_message = await configured["agent_takeover"]("1", "Tests")
    unrelated_message = await configured["agent_takeover"](
        "1", "Tests", "Marco says: continue the current task.",
    )

    assert (missing_message.status, missing_message.reason) == (
        "denied", "takeover_confirmation_required",
    )
    assert missing_message.next_action is not None
    assert "Takeover was not performed" in missing_message.next_action
    assert "Ask Marco" in missing_message.next_action
    assert "'Tests'" in missing_message.next_action
    assert (unrelated_message.status, unrelated_message.reason) == (
        "denied", "takeover_confirmation_required",
    )


@pytest.mark.parametrize("message", [
    "Take over Alpha, not Tests",
    "Take over Testsuite",
    "Do not take over Tests",
    "No takeover Tests",
    "Should you take over Tests?",
    "Take over Alpha and take over Tests",
    "Take over Tests and Alpha",
    "Take over Tests, Alpha",
    "Refuse to take over Tests.",
    "Alice should take over Tests.",
    "Take over Tests; and Alpha",
    "Take over Tests: Alpha",
])
async def test_named_cross_principal_takeover_requires_one_exact_affirmative_directive(
    agent_messaging, message,
):
    tools, actor, session, _owner, other, service = agent_messaging
    assert (await tools["agent_register"]("1", "Tests")).status == "ok"
    actor[0], session[0] = other, "replacement-chat"

    result = await root_tools(service, session)["agent_takeover"](
        "1", "Tests", message,
    )

    assert (result.status, result.reason) == (
        "denied", "takeover_confirmation_required",
    )
    assert result.next_action is not None


@pytest.mark.parametrize("takeover_message", [
    "Please takeover coordinator.",
    "Take over as coordinator.",
])
async def test_coordinator_takeover_canonicalizes_root_and_keeps_mail(
    agent_messaging, takeover_message,
):
    tools, actor, session, _owner, other, service = agent_messaging
    await bind_root(tools, service)
    assert service.messages is not None
    before = await AgentMailboxState(service.messages.engine).by_name("/root")
    assert before.mailbox is not None
    session[0] = "sender-chat"
    assert (await tools["agent_register"]("1", "Sender")).status == "ok"
    sent = await tools["agent_message_send"](
        "1", "/root", uuid4(), {"request": "kept"}
    )
    assert sent.status == "ok" and sent.message is not None

    actor[0], session[0] = other, "replacement-chat"
    configured = root_tools(service, session)
    moved = await configured["agent_takeover"](
        "1", CODEX_COORDINATOR_NAME, takeover_message,
    )

    assert (moved.status, moved.name) == ("ok", CODEX_COORDINATOR_NAME)
    pending = await configured["agent_message_pending"]("1")
    assert [item.delivery_id for item in pending.messages] == [sent.message.delivery_id]

    canonical = await AgentMailboxState(service.messages.engine).by_name(
        CODEX_COORDINATOR_NAME
    )
    legacy_alias = await AgentMailboxState(service.messages.engine).by_name("/root")
    assert canonical == legacy_alias
    assert canonical.mailbox is not None
    assert canonical.mailbox.endpoint_id == before.mailbox.endpoint_id
    assert canonical.mailbox.generation == before.mailbox.generation + 1
    assert canonical.mailbox.name_key == CODEX_COORDINATOR_KEY


async def test_legacy_root_sender_routes_to_canonical_coordinator(agent_messaging):
    tools, actor, session, _owner, other, service = agent_messaging
    await bind_root(tools, service)
    actor[0], session[0] = other, "replacement-chat"
    configured = root_tools(service, session)
    assert (await configured["agent_takeover"](
        "1", "/root", "takeover /root"
    )).name == CODEX_COORDINATOR_NAME

    session[0] = "sender-chat"
    assert (await configured["agent_register"]("1", "Sender")).status == "ok"
    sent = await configured["agent_message_send"](
        "1", "/root", uuid4(), {"request": "legacy route"}
    )
    assert sent.status == "ok" and sent.message is not None
    assert sent.message.recipient_name == CODEX_COORDINATOR_NAME


async def test_cross_principal_coordinator_takeover_requires_confirmation(agent_messaging):
    tools, actor, session, _owner, other, service = agent_messaging
    await bind_root(tools, service)
    actor[0], session[0] = other, "replacement-chat"

    result = await root_tools(service, session)["agent_takeover"](
        "1", CODEX_COORDINATOR_NAME,
    )

    assert (result.status, result.reason) == (
        "denied", "takeover_confirmation_required",
    )
    assert result.next_action is not None
    assert "Takeover was not performed" in result.next_action
    assert "Ask Marco" in result.next_action


async def test_same_principal_coordinator_takeover_needs_no_confirmation(agent_messaging):
    tools, _actor, session, _owner, _other, service = agent_messaging
    await bind_root(tools, service)
    session[0] = "replacement-chat"

    result = await root_tools(service, session)["agent_takeover"](
        "1", CODEX_COORDINATOR_NAME,
    )

    assert (result.status, result.name) == ("ok", CODEX_COORDINATOR_NAME)


async def test_coordinator_takeover_rechecks_principal_after_forced_interleaving(
    agent_messaging, monkeypatch,
):
    tools, _actor, session, owner, other, service = agent_messaging
    await bind_root(tools, service)
    session[0] = "replacement-chat"
    original = AgentMailboxState.takeover_coordinator
    interleaved = False

    async def owner_changes_before_locked_takeover(
        self, principal_key, replacement_session, *, allow_principal_change=False,
    ):
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            moved = await original(
                self, other.key, "interloper-chat", allow_principal_change=True,
            )
            assert moved.status == "ok"
        return await original(
            self, principal_key, replacement_session,
            allow_principal_change=allow_principal_change,
        )

    monkeypatch.setattr(
        AgentMailboxState, "takeover_coordinator", owner_changes_before_locked_takeover,
    )
    result = await root_tools(service, session)["agent_takeover"](
        "1", CODEX_COORDINATOR_NAME,
    )

    assert interleaved
    assert (result.status, result.reason) == (
        "denied", "takeover_confirmation_required",
    )
    assert service.messages is not None
    current = await AgentMailboxState(service.messages.engine).for_actor(
        other.key, "interloper-chat",
    )
    assert current.status == "ok" and current.mailbox is not None
    assert current.mailbox.name_key == CODEX_COORDINATOR_KEY
    assert (await AgentMailboxState(service.messages.engine).for_actor(
        owner.key, "replacement-chat",
    )).reason == "agent_not_registered"


@pytest.mark.parametrize(
    "message",
    ["move /root", "takeover root"],
)
async def test_root_takeover_rejects_a_missing_required_word(agent_messaging, message):
    tools, actor, session, _owner, other, service = agent_messaging
    await bind_root(tools, service)
    actor[0], session[0] = other, "replacement-chat"

    result = await root_tools(service, session)["agent_takeover"](
        "1", "/root", message
    )

    assert (result.status, result.reason) == (
        "denied", "takeover_confirmation_required",
    )


async def test_root_takeover_fences_the_old_session(agent_messaging):
    tools, actor, session, _owner, other, service = agent_messaging
    await bind_root(tools, service)
    configured = root_tools(service, session)
    actor[0], session[0] = other, "replacement-chat"
    assert (await configured["agent_takeover"](
        "1", "/root", "takeover /root"
    )).status == "ok"

    actor[0], session[0] = _owner, "chat-a"
    old = await configured["agent_message_pending"]("1")
    assert (old.status, old.reason) == ("denied", "agent_not_registered")


async def test_root_takeover_does_not_audit_or_log_confirmation_message(
    agent_messaging, caplog,
):
    tools, actor, session, _owner, other, service = agent_messaging
    await bind_root(tools, service)
    actor[0], session[0] = other, "replacement-chat"
    records = []
    configured = root_tools(service, session, audit=lambda *record: records.append(record))
    message = "Marco says: takeover /root. private-message-marker"

    assert (await configured["agent_takeover"]("1", "/root", message)).status == "ok"

    evidence = repr(records) + caplog.text
    assert records == [("agent_takeover", "/root", "ok")]
    assert "private-message-marker" not in evidence


async def test_root_takeover_ambiguous_result_does_not_overwrite_newer_owner(
    agent_messaging, monkeypatch,
):
    tools, actor, session, owner, other, service = agent_messaging
    await bind_root(tools, service)
    actor[0], session[0] = other, "replacement-chat"
    configured = root_tools(service, session)
    original = AgentMailboxState.takeover_coordinator
    calls = 0

    async def ambiguous_then_newer(
        self, principal_key, replacement_session, *, allow_principal_change=False,
    ):
        nonlocal calls
        calls += 1
        stored = await original(
            self, principal_key, replacement_session,
            allow_principal_change=allow_principal_change,
        )
        assert stored.status == "ok"
        newer = await original(
            self, owner.key, "newer-chat", allow_principal_change=True,
        )
        assert newer.status == "ok"
        return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    monkeypatch.setattr(AgentMailboxState, "takeover_coordinator", ambiguous_then_newer)

    result = await configured["agent_takeover"](
        "1", "/root", "takeover /root"
    )

    assert (result.status, result.reason) == ("recovery_required", "state_unavailable")
    assert calls == 1
    assert service.messages is not None
    observed = await AgentMailboxState(service.messages.engine).for_actor(
        owner.key, "newer-chat"
    )
    assert observed.status == "ok" and observed.mailbox is not None
    assert observed.mailbox.name_key == CODEX_COORDINATOR_KEY


async def test_takeover_retries_ambiguous_state_once_and_reads_back_binding(
    agent_messaging, monkeypatch,
):
    tools, _actor, session, _owner, _other, _service = agent_messaging
    assert (await tools["agent_register"]("1", "Coordinator")).status == "ok"
    session[0] = "replacement"
    original_takeover = AgentMailboxState.takeover
    original_for_actor = AgentMailboxState.for_actor
    calls: list[tuple[str, str, str]] = []
    readbacks = 0

    async def ambiguous_once(self, name, principal_key, replacement_session):
        calls.append((name, principal_key, replacement_session))
        stored = await original_takeover(self, name, principal_key, replacement_session)
        if len(calls) == 1:
            assert stored.status == "ok"  # The commit happened; its response was lost.
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")
        return stored

    async def observed(self, principal_key, chat_session):
        nonlocal readbacks
        readbacks += 1
        return await original_for_actor(self, principal_key, chat_session)

    monkeypatch.setattr(AgentMailboxState, "takeover", ambiguous_once)
    monkeypatch.setattr(AgentMailboxState, "for_actor", observed)

    result = await tools["agent_takeover"]("1", "Coordinator")

    assert (result.status, result.name) == ("ok", "Coordinator")
    assert len(calls) == 2 and calls[0] == calls[1]
    assert readbacks == 1


async def test_takeover_does_not_retry_explicit_denial(agent_messaging, monkeypatch):
    tools, actor, session, _owner, other, _service = agent_messaging
    assert (await tools["agent_register"]("1", "Coordinator")).status == "ok"
    actor[0], session[0] = other, "other-session"
    original = AgentMailboxState.takeover
    calls = 0

    async def counted(self, name, principal_key, replacement_session):
        nonlocal calls
        calls += 1
        return await original(self, name, principal_key, replacement_session)

    monkeypatch.setattr(AgentMailboxState, "takeover", counted)

    result = await tools["agent_takeover"]("1", "Coordinator")

    assert (result.status, result.reason) == (
        "denied", "takeover_confirmation_required",
    )
    assert result.next_action is not None
    assert calls == 1


async def test_takeover_retry_does_not_claim_success_without_binding_readback(
    agent_messaging, monkeypatch,
):
    tools, _actor, session, _owner, _other, _service = agent_messaging
    assert (await tools["agent_register"]("1", "Coordinator")).status == "ok"
    session[0] = "replacement"
    original = AgentMailboxState.takeover
    calls = 0

    async def ambiguous_once(self, name, principal_key, replacement_session):
        nonlocal calls
        calls += 1
        stored = await original(self, name, principal_key, replacement_session)
        if calls == 1:
            assert stored.status == "ok"
            return AgentMailboxResult(status="recovery_required", reason="state_unavailable")
        return stored

    async def unavailable_readback(self, principal_key, chat_session):
        return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    monkeypatch.setattr(AgentMailboxState, "takeover", ambiguous_once)
    monkeypatch.setattr(AgentMailboxState, "for_actor", unavailable_readback)

    result = await tools["agent_takeover"]("1", "Coordinator")

    assert (result.status, result.reason) == ("recovery_required", "state_unavailable")
    assert calls == 2


async def test_host_approved_transfer_preserves_delivery_and_fences_old_owner(agent_messaging):
    tools, actor, session, _owner, other, service = agent_messaging
    assert (await tools["agent_register"]("1", "Alpha")).status == "ok"
    session[0] = "chat-b"
    assert (await tools["agent_register"]("1", "Coordinator")).status == "ok"
    session[0] = "chat-a"
    sent = await tools["agent_message_send"](
        "1", "Coordinator", uuid4(), {"request": "handoff"},
    )
    assert sent.status == "ok" and sent.message is not None
    delivery = sent.message.delivery_id

    actor[0], session[0] = other, "new-coordinator-chat"
    request = await tools["agent_transfer_request"]("1", "Coordinator")
    assert request.status == "ok" and request.request_id is not None
    assert service.messages is not None
    approval = await AgentMailboxState(service.messages.engine).approve_transfer(request.request_id)
    assert approval.status == "approved"

    recovered = await tools["agent_message_receive"]("1", delivery)
    assert recovered.status == "ok" and recovered.state == "RECEIVED"
    session[0] = "chat-b"
    old = await tools["agent_message_pending"]("1")
    assert (old.status, old.reason) == ("denied", "agent_not_registered")


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
    assert (await tools["agent_message_recover"]("1", delivery)).status == "conflict"
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


async def test_payload_boundary_and_bounded_legacy_replay(agent_messaging):
    tools, _actor, session, _owner, _other, service = agent_messaging
    assert (await tools["agent_register"]("1", "Alpha")).status == "ok"
    session[0] = "chat-b"
    assert (await tools["agent_register"]("1", "Beta")).status == "ok"

    session[0] = "chat-a"
    accepted_id, rejected_id = uuid4(), uuid4()
    accepted = await tools["agent_message_send"]("1", "Beta", accepted_id, "a" * 798)
    rejected = await tools["agent_message_send"]("1", "Beta", rejected_id, "a" * 799)
    assert accepted.status == "ok" and accepted.message.payload == "a" * 798
    StrictPreDeploySubmitResult.model_validate(accepted.model_dump(mode="json"))
    assert (rejected.status, rejected.reason) == ("denied", "payload_too_large")
    assert "exact tracker WorkId" in rejected.next_action
    assert service.messages is not None
    async with service.messages.engine.connect() as connection:
        assert await connection.scalar(select(messages.c.message_id).where(
            messages.c.message_id == rejected_id
        )) is None

    # Pre-existing oversized rows remain replayable, but their public view is bounded.
    alpha = await AgentMailboxState(service.messages.engine).by_name("Alpha")
    beta = await AgentMailboxState(service.messages.engine).by_name("Beta")
    assert alpha.mailbox is not None and beta.mailbox is not None
    legacy_id, legacy_delivery = uuid4(), uuid4()
    legacy_payload = "z" * 900
    async with service.messages.engine.begin() as connection:
        await connection.execute(insert(messages).values(
            sender_work_id=alpha.mailbox.endpoint_id, message_id=legacy_id,
            route_ref="agent.beta", kind="request", payload=legacy_payload,
            digest="legacy", in_reply_to_delivery_id=None,
        ))
        await connection.execute(insert(message_deliveries).values(
            delivery_id=legacy_delivery, sender_work_id=alpha.mailbox.endpoint_id,
            message_id=legacy_id, recipient_work_id=beta.mailbox.endpoint_id,
            recipient_grant_version=beta.mailbox.generation,
        ))
    replay = await tools["agent_message_send"]("1", "Beta", legacy_id, legacy_payload)
    assert replay.status == "ok" and replay.message.payload is None
    StrictPreDeploySubmitResult.model_validate(replay.model_dump(mode="json"))
    session[0] = "chat-b"
    pending = await tools["agent_message_pending"]("1")
    legacy = next(item for item in pending.messages if item.message_id == legacy_id)
    assert legacy.payload is None
    StrictPreDeployPendingResult.model_validate(pending.model_dump(mode="json"))


async def test_pending_default_page_is_ten_with_keyset_continuation(agent_messaging):
    tools, _actor, session, _owner, _other, _service = agent_messaging
    assert (await tools["agent_register"]("1", "Alpha")).status == "ok"
    session[0] = "chat-b"
    assert (await tools["agent_register"]("1", "Beta")).status == "ok"
    session[0] = "chat-a"
    for index in range(11):
        assert (await tools["agent_message_send"](
            "1", "Beta", uuid4(), {"index": index}
        )).status == "ok"
    session[0] = "chat-b"
    first = await tools["agent_message_pending"]("1")
    assert len(first.messages) == 10 and first.has_more and first.next_cursor is not None
    second = await tools["agent_message_pending"]("1", first.next_cursor)
    assert len(second.messages) == 1 and not second.has_more


async def test_fresh_pending_poll_finds_delivery_hidden_by_older_page_cursor(agent_messaging):
    tools, _actor, session, _owner, _other, _service = agent_messaging
    assert (await tools["agent_register"]("1", "Alpha")).status == "ok"
    session[0] = "chat-b"
    assert (await tools["agent_register"]("1", "Beta")).status == "ok"

    session[0] = "chat-a"
    for index in range(2):
        assert (await tools["agent_message_send"](
            "1", "Beta", uuid4(), {"index": index}
        )).status == "ok"

    session[0] = "chat-b"
    initial = await tools["agent_message_pending"]("1", limit=1)
    assert initial.has_more and initial.next_cursor is not None

    session[0] = "chat-a"
    new_message_id = uuid4()
    assert (await tools["agent_message_send"](
        "1", "Beta", new_message_id, {"index": "new"}
    )).status == "ok"

    session[0] = "chat-b"
    older_page = await tools["agent_message_pending"]("1", initial.next_cursor)
    fresh_page = await tools["agent_message_pending"]("1")
    assert new_message_id not in {message.message_id for message in older_page.messages}
    assert new_message_id in {message.message_id for message in fresh_page.messages}


async def test_codex_stop_page_uses_current_session_binding_without_receiving(agent_messaging):
    tools, _actor, session, _owner, _other, service = agent_messaging
    session[0] = "chat-a"
    assert (await tools["agent_register"]("1", "Alpha")).status == "ok"
    session[0] = "codex:thread-1"
    assert (await tools["agent_register"]("1", "Beta")).status == "ok"
    session[0] = "chat-a"
    sent = await tools["agent_message_send"](
        "1", "Beta", uuid4(), {"request": "act"}
    )
    assert sent.message is not None

    page = await _codex_stop_mailbox_page(service, "thread-1", None)
    assert page == {
        "status": "ok",
        "delivery_ids": [str(sent.message.delivery_id)],
        "has_more": False,
        "next_cursor": None,
    }
    session[0] = "codex:thread-2"
    assert (await tools["agent_takeover"]("1", "Beta")).status == "ok"
    assert await _codex_stop_mailbox_page(service, "thread-1", None) == {
        "status": "denied", "reason": "agent_not_registered",
    }


async def test_acknowledge_batch_is_per_delivery_and_preserves_open_reply(agent_messaging):
    tools, _actor, session, _owner, _other, service = agent_messaging
    assert (await tools["agent_register"]("1", "Alpha")).status == "ok"
    session[0] = "chat-b"
    assert (await tools["agent_register"]("1", "Beta")).status == "ok"
    session[0] = "chat-c"
    assert (await tools["agent_register"]("1", "Gamma")).status == "ok"

    session[0] = "chat-a"
    request_id = uuid4()
    tracked_work_id = uuid4()
    sent = await tools["agent_message_send"](
        "1", "Beta", request_id, {"work_id": str(tracked_work_id), "request": "act"}
    )
    session[0] = "chat-c"
    foreign = await tools["agent_message_send"]("1", "Alpha", uuid4(), {"request": "x"})
    assert sent.message is not None and foreign.message is not None

    assert service.messages is not None
    beta = await AgentMailboxState(service.messages.engine).by_name("Beta")
    alpha = await AgentMailboxState(service.messages.engine).by_name("Alpha")
    assert beta.mailbox is not None and alpha.mailbox is not None
    review_id, review_delivery = uuid4(), uuid4()
    async with service.messages.engine.begin() as connection:
        await connection.execute(insert(messages).values(
            sender_work_id=alpha.mailbox.endpoint_id, message_id=review_id,
            route_ref="review.request", kind="request", payload={"review": "typed"},
            digest="typed", in_reply_to_delivery_id=None,
        ))
        await connection.execute(insert(message_deliveries).values(
            delivery_id=review_delivery, sender_work_id=alpha.mailbox.endpoint_id,
            message_id=review_id, recipient_work_id=beta.mailbox.endpoint_id,
            recipient_grant_version=beta.mailbox.generation,
        ))

    session[0] = "chat-b"
    missing = uuid4()
    mixed = await tools["agent_message_acknowledge"](
        "1", [sent.message.delivery_id, foreign.message.delivery_id, missing, review_delivery]
    )
    assert mixed.status == "ok"
    assert [(item.status, item.reason) for item in mixed.results] == [
        ("ok", None), ("denied", "delivery_not_for_current_work"),
        ("denied", "delivery_not_found"), ("denied", "reserved_route"),
    ]
    repeated = await tools["agent_message_acknowledge"]("1", [sent.message.delivery_id])
    assert repeated.results[0].state == "ACKNOWLEDGED"
    acknowledged_receive = await tools["agent_message_receive"](
        "1", sent.message.delivery_id
    )
    assert (acknowledged_receive.status, acknowledged_receive.reason) == (
        "conflict", "delivery_already_acknowledged",
    )
    assert await service.messages.open_handoff_watches(tracked_work_id) == (
        f"message:{sent.message.delivery_id}",
    )
    assert all(item.message_id != request_id for item in (
        await tools["agent_message_pending"]("1")
    ).messages)

    session[0] = "chat-a"
    replay = await tools["agent_message_send"](
        "1", "Beta", request_id, sent.message.payload
    )
    assert replay.message is not None and replay.message.state == "RECEIVED"
    StrictPreDeploySubmitResult.model_validate(replay.model_dump(mode="json"))
    assert replay.next_action is not None
    session[0] = "beta-new"
    assert (await tools["agent_takeover"]("1", "Beta")).status == "ok"
    session[0] = "chat-b"
    stale_reply = await tools["agent_message_result_send"](
        "1", sent.message.delivery_id, uuid4(), {"result": "stale"}
    )
    assert (stale_reply.status, stale_reply.reason) == ("denied", "agent_not_registered")
    session[0] = "beta-new"
    result_id = uuid4()
    result = await tools["agent_message_result_send"](
        "1", sent.message.delivery_id, result_id, {"result": "done"}
    )
    assert result.status == "ok"
    async with service.messages.engine.connect() as connection:
        row = (await connection.execute(select(
            message_deliveries.c.state, message_deliveries.c.evidence,
        ).where(
            message_deliveries.c.delivery_id == sent.message.delivery_id
        ))).one()
    assert row == ("DISPOSITIONED", {"kind": "result", "result_message_id": str(result_id),
                                     "operation_id": None})
    # A legacy crash could leave the exact result committed without its disposition.
    async with service.messages.engine.begin() as connection:
        await connection.execute(update(message_deliveries).where(
            message_deliveries.c.delivery_id == sent.message.delivery_id
        ).values(
            state="ACKNOWLEDGED", dispositioned_at=None,
            disposition_digest=None, evidence=None,
        ))
    replayed_result = await tools["agent_message_result_send"](
        "1", sent.message.delivery_id, result_id, {"result": "done"}
    )
    assert replayed_result.status == "ok"
    async with service.messages.engine.connect() as connection:
        assert await connection.scalar(select(message_deliveries.c.state).where(
            message_deliveries.c.delivery_id == sent.message.delivery_id
        )) == "DISPOSITIONED"


async def test_result_and_disposition_roll_back_together(agent_messaging, monkeypatch):
    tools, _actor, session, _owner, _other, service = agent_messaging
    assert (await tools["agent_register"]("1", "Alpha")).status == "ok"
    session[0] = "chat-b"
    assert (await tools["agent_register"]("1", "Beta")).status == "ok"
    session[0] = "chat-a"
    sent = await tools["agent_message_send"]("1", "Beta", uuid4(), {"request": "atomic"})
    assert sent.message is not None
    session[0] = "chat-b"
    assert (await tools["agent_message_receive"]("1", sent.message.delivery_id)).status == "ok"
    assert service.messages is not None

    async def fail_disposition(*_args, **_kwargs):
        raise SQLAlchemyError("injected disposition failure")

    monkeypatch.setattr(service.messages, "_disposition_result", fail_disposition)
    result_id = uuid4()
    failed = await tools["agent_message_result_send"](
        "1", sent.message.delivery_id, result_id, {"result": "no commit"}
    )
    assert (failed.status, failed.reason) == ("recovery_required", "state_unavailable")
    async with service.messages.engine.connect() as connection:
        assert await connection.scalar(select(messages.c.message_id).where(
            messages.c.message_id == result_id
        )) is None
        assert await connection.scalar(select(message_deliveries.c.state).where(
            message_deliveries.c.delivery_id == sent.message.delivery_id
        )) == "RECEIVED"
