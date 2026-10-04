import asyncio
import json
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.exc import SQLAlchemyError

from switchstand.agent_mailboxes import AgentMailboxState, agent_mailboxes, chat_session_key
from switchstand.codex_wakeful import (
    Projection,
    WakeSourceRef,
    inbound_cycle,
    projection_lock,
    run_inbound,
    wake_id,
)
from switchstand.messages import (
    MessageRoute,
    MessageState,
    MessageSubmitRequest,
    message_deliveries,
)

# Reuse the existing disposable PostgreSQL and host-boundary fixtures.
pytest_plugins = ["test_codex_wakeful", "test_messages"]


async def source(subject, session="codex:exact"):
    messages, engine, _grants, principal, sender, _, _ = subject
    mailboxes = AgentMailboxState(engine)
    registered = await mailboxes.register_agent("pilot-root", principal.key, session)
    mailbox = registered.mailbox
    assert mailbox is not None
    request = MessageSubmitRequest(api_version="1", message_id=uuid4(), grant_version=1,
        route_ref="agent.pilot-root", kind="request", payload="private-source-payload")
    submitted = await messages.submit_admitted(sender.authority.active_work_id,
        MessageRoute(recipient_work_id=mailbox.endpoint_id,
                     recipient_grant_version=mailbox.generation), request)
    assert submitted.status == "ok" and submitted.message is not None
    return messages, mailboxes, mailbox, submitted.message.delivery_id


async def test_committed_source_restart_exact_reference_and_duplicate(subject, setup):
    home, _, client, binding = setup
    messages, mailboxes, mailbox, delivery_id = await source(subject)
    restarted = MessageState(messages.engine, messages.grants)
    ids = await restarted.pending_delivery_ids(mailbox)
    assert ids == (delivery_id,)
    cursor, results = await inbound_cycle(
        restarted, mailboxes, mailbox, Projection(home, binding), client)
    assert cursor is None and set(results.values()) == {"PENDING"}
    client.consume()
    _, results = await inbound_cycle(
        restarted, mailboxes, mailbox, Projection(home, binding), client)
    assert set(results.values()) == {"ADMITTED"} and len(client.calls) == 1
    assert str(delivery_id) in client.calls[0]["input"][0]["text"]
    assert "private-source-payload" not in json.dumps(client.calls)
    assert "private-source-payload" not in (home / "codex-wakeful.json").read_text()
    other = await mailboxes.register_agent("other-root", mailbox.principal_key, "codex:other")
    assert other.mailbox is not None
    assert await messages.pending_delivery_ids(other.mailbox) == ()


async def test_disposition_between_scan_and_admission_does_not_wake(subject, setup, monkeypatch):
    home, _, client, binding = setup
    messages, mailboxes, mailbox, delivery_id = await source(subject)
    scan = messages.pending_delivery_ids

    async def transitioned(selected, cursor=None):
        ids = await scan(selected, cursor)
        async with messages.engine.begin() as connection:
            await connection.execute(update(message_deliveries).where(
                message_deliveries.c.delivery_id == delivery_id
            ).values(state="DISPOSITIONED"))
        return ids

    monkeypatch.setattr(messages, "pending_delivery_ids", transitioned)
    _, results = await inbound_cycle(messages, mailboxes, mailbox, Projection(home, binding), client)
    assert set(results.values()) == {"STALE"} and not client.calls
    assert not (home / "codex-wakeful.json").exists()


async def test_source_lifecycle_progresses_while_host_admission_waits(subject, setup, monkeypatch):
    home, _, client, binding = setup
    messages, mailboxes, mailbox, delivery_id = await source(subject)
    sync = create_engine(messages.engine.url)
    finished, failures = threading.Event(), []

    def transition():
        try:
            with sync.begin() as connection:
                connection.execute(text("SET LOCAL lock_timeout = '500ms'"))
                connection.execute(update(message_deliveries).where(
                    message_deliveries.c.delivery_id == delivery_id
                ).values(state="DISPOSITIONED"))
                connection.execute(update(agent_mailboxes).where(
                    agent_mailboxes.c.endpoint_id == mailbox.endpoint_id
                ).values(generation=mailbox.generation + 1,
                         session_key=chat_session_key("codex:replacement")))
        except SQLAlchemyError as exc:
            failures.append(type(exc).__name__)
        finally:
            finished.set()

    def waiting_host(*_args, **_kwargs):
        worker = threading.Thread(target=transition)
        worker.start()
        try:
            assert finished.wait(3), "source lifecycle blocked by host admission"
            assert not failures, "source lifecycle could not acquire canonical locks"
        finally:
            worker.join(timeout=3)
        return "UNKNOWN"

    monkeypatch.setattr(Projection, "admit", waiting_host)
    try:
        await inbound_cycle(messages, mailboxes, mailbox, Projection(home, binding), client)
        assert await messages.pending_delivery_ids(mailbox) is None
    finally:
        sync.dispose()


async def test_exact_mailbox_session_and_takeover_fence(subject, setup):
    home, _, client, binding = setup
    messages, mailboxes, mailbox, delivery_id = await source(subject, "codex:other")
    assert await inbound_cycle(messages, mailboxes, mailbox, Projection(home, binding), client) == (
        None, {"source": "STALE"})
    takeover = await mailboxes.takeover(mailbox.name, mailbox.principal_key, "codex:exact")
    assert takeover.mailbox is not None and takeover.mailbox.generation == mailbox.generation + 1
    assert await messages.pending_delivery_ids(mailbox) is None
    assert not await messages.pending_delivery(mailbox, delivery_id)
    assert await inbound_cycle(messages, mailboxes, mailbox, Projection(home, binding), client) == (
        None, {"source": "STALE"})
    assert not client.calls


@pytest.mark.parametrize("state", ["active", "unsupported"])
async def test_busy_queue_and_unsupported_history_fail_safely(subject, setup, state):
    home, _, client, binding = setup
    messages, mailboxes, mailbox, delivery_id = await source(subject)
    if state == "active":
        client.thread["status"] = {"type": "active", "activeFlags": []}
    else:
        client.thread["historyMode"] = "structured"
    _, results = await inbound_cycle(messages, mailboxes, mailbox, Projection(home, binding), client)
    assert set(results.values()) == ({"PENDING"} if state == "active" else {"UNKNOWN"})
    assert len(client.calls) == (1 if state == "active" else 0)
    assert await messages.pending_delivery_ids(mailbox) == (delivery_id,)
    identity = wake_id(binding, WakeSourceRef("switchstand_inbound", str(delivery_id)))
    assert json.loads((home / "codex-wakeful.json").read_text())[identity]["attempted"] is (
        state == "active")


async def test_default_off_and_stop_before_source_or_host_access(subject, setup):
    home, _, _, binding = setup
    messages, mailboxes, mailbox, _ = await source(subject)
    stop = asyncio.Event()
    await run_inbound(messages, mailboxes, mailbox, binding, home, home / "missing", stop)
    assert not (home / "codex-wakeful.lock").exists()
    stop.set()
    await run_inbound(messages, mailboxes, mailbox, binding, home, home / "missing", stop,
                      opt_in=True)
    assert not (home / "codex-wakeful.json").exists()


async def test_supervised_intake_scans_real_source_without_agent_poll(subject, setup, monkeypatch):
    home, _, client, binding = setup
    messages, mailboxes, mailbox, _ = await source(subject)
    admitted, stop = asyncio.Event(), asyncio.Event()
    original_call = client.call

    def observed(method, params):
        result = original_call(method, params)
        if method == "thread/queue/add":
            admitted.set()
        return result

    monkeypatch.setattr(client, "call", observed)
    monkeypatch.setattr(client, "close", lambda: None)
    monkeypatch.setattr("switchstand.codex_wakeful.QueueClient", lambda *_: client)
    task = asyncio.create_task(run_inbound(
        messages, mailboxes, mailbox, binding, home, home / "unused", stop, opt_in=True))
    try:
        await asyncio.wait_for(admitted.wait(), timeout=3)
        assert len(client.calls) == 1
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=3)


def test_projection_lock_excludes_other_generations_and_rejects_symlink(setup):
    home, token, _, _ = setup
    with projection_lock(home), pytest.raises(BlockingIOError), projection_lock(home):
        pytest.fail("second writer admitted")
    with projection_lock(home):
        pass
    (home / "codex-wakeful.lock").unlink()
    (home / "codex-wakeful.lock").symlink_to(token)
    with pytest.raises(OSError), projection_lock(home):
        pytest.fail("symlink lock admitted")
