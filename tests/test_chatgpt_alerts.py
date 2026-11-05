"""Isolated real-store proof; no live actor or service is provisioned."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.agent_mailboxes import AgentMailboxState, agent_mailboxes, chat_session_key
from switchstand.chatgpt_alerts import (
    RECIPIENT_ENDPOINT,
    AlertReport,
    HostReporter,
    bootstrap,
    plan,
    read_manifest,
    revoke,
)
from switchstand.grant_state import GrantState
from switchstand.messages import MessageState, message_deliveries
from switchstand.state import metadata as state_metadata


def test_manifest_hash_and_fixed_scope(tmp_path):
    value = plan("a" * 40)
    path = tmp_path / "manifest.json"
    raw = value.model_dump_json().encode()
    path.write_bytes(raw)
    import hashlib
    assert read_manifest(path, hashlib.sha256(raw).hexdigest()) == value
    with pytest.raises(ValueError, match="hash mismatch"):
        read_manifest(path, "0" * 64)
    assert value.grant.operations == {"message"}
    assert not value.grant.can_write(uuid4())
    assert value.recipient == "ChatGPT Wake Setup 2026-11-04"


@pytest.mark.parametrize("reason", ["draft-present", "pending-mail"])
async def test_bootstrap_interruption_and_exact_alert_replay(state_database_schema, monkeypatch, reason):
    engine = create_async_engine(state_database_schema)
    async with engine.begin() as connection:
        await connection.run_sync(state_metadata.drop_all)
        await connection.run_sync(state_metadata.create_all)
    grants = GrantState(engine)
    mailboxes = AgentMailboxState(engine)
    manifest = plan("b" * 40)
    original = AgentMailboxState.register_agent

    async def interrupted(*args, **kwargs):
        raise RuntimeError("lost after grant commit")

    monkeypatch.setattr(AgentMailboxState, "register_agent", interrupted)
    with pytest.raises(RuntimeError, match="lost after grant"):
        await bootstrap(manifest, grants, issue=True)
    assert await grants.current(manifest.principal.key) == manifest.grant
    monkeypatch.setattr(AgentMailboxState, "register_agent", original)
    sender = await bootstrap(manifest, grants, issue=True)
    assert await bootstrap(manifest, grants, issue=True) == sender
    assert await bootstrap(manifest, grants, issue=False) == sender
    assert (await grants.current(manifest.principal.key)).version == 1
    with pytest.raises(ValueError, match="collision"):
        await bootstrap(plan("b" * 40), grants, issue=True)
    with pytest.raises(ValueError, match="differs"):
        await bootstrap(manifest.model_copy(update={"grant_id": uuid4()}), grants, issue=True)

    # The reserved recipient is a real fixture registry row, never a takeover of a live role.
    async with engine.begin() as connection:
        await connection.execute(agent_mailboxes.insert().values(
            name_key="browser-extension", display_name="browser extension", endpoint_id=RECIPIENT_ENDPOINT,
            principal_key="fixture-coordinator", session_key=chat_session_key("fixture"), generation=1))
    target = (await mailboxes.register_agent("Fixture tracker", "fixture-target", "fixture-chat")).mailbox
    assert target
    state = MessageState(engine, grants)
    from switchstand.agent_messages import send_agent_request
    sent = await send_agent_request(mailboxes, state, sender, target.name, uuid4(), {"test": True})
    assert sent.message
    delivery = sent.message.delivery_id
    now = datetime.now(UTC)
    evidence = AlertReport(incident_id=uuid4(), phase="blocked", name=target.name,
        endpoint=target.endpoint_id, generation=1, delivery_ids=[delivery], reason=reason,
        started_at=now - timedelta(seconds=60), observed_at=now)
    payload = evidence.payload()
    assert set(payload) == {"type", "phase", "incident", "mailbox", "reason", "work_id", "pending_count", "evidence_sha256"}
    assert payload["reason"] == reason
    reporter = HostReporter(manifest, state)
    premature = evidence.model_copy(update={"started_at": now - timedelta(seconds=59), "incident_id": uuid4()})
    assert (await reporter.send(premature))["reason"] == "unqualified_observation"
    wrong = evidence.model_copy(update={"delivery_ids": [uuid4()], "incident_id": uuid4()})
    assert (await reporter.send(wrong))["reason"] == "pending_evidence_changed"
    expired = evidence.model_copy(update={"incident_id": uuid4(),
        "started_at": now - timedelta(seconds=152), "observed_at": now - timedelta(seconds=91)})
    assert await reporter.send(expired) == {"status": "denied", "reason": "observation_expired"}
    async with engine.begin() as connection:
        await connection.execute(update(agent_mailboxes).where(
            agent_mailboxes.c.endpoint_id == RECIPIENT_ENDPOINT).values(generation=2))
    assert (await reporter.send(evidence))["reason"] == "recipient_binding_changed"
    async with engine.begin() as connection:
        await connection.execute(update(agent_mailboxes).where(
            agent_mailboxes.c.endpoint_id == RECIPIENT_ENDPOINT).values(generation=1))
    # Change the actual send lookup after the standalone precheck. Both kinds of
    # movement must fail before any delivery, rather than trusting the old check.
    original_lookup = AgentMailboxState.by_name
    for movement in ({"generation": 2}, {"endpoint_id": uuid4()}):
        calls = 0

        async def moved_lookup(self, name, movement=movement):
            nonlocal calls
            if name == "browser extension":
                calls += 1
                if calls == 2:
                    async with engine.begin() as connection:
                        await connection.execute(update(agent_mailboxes).where(
                            agent_mailboxes.c.name_key == "browser-extension").values(**movement))
            return await original_lookup(self, name)

        with monkeypatch.context() as patch:
            patch.setattr(AgentMailboxState, "by_name", moved_lookup)
            assert (await reporter.send(evidence))["reason"] == "recipient_binding_changed"
        async with engine.begin() as connection:
            assert len((await connection.execute(select(message_deliveries))).all()) == 1
            await connection.execute(update(agent_mailboxes).where(
                agent_mailboxes.c.name_key == "browser-extension").values(
                    endpoint_id=RECIPIENT_ENDPOINT, generation=1))
    receipt = await reporter.send(evidence)  # Simulate its response being lost to the extension.
    assert receipt["status"] == "queued"
    async with engine.connect() as connection:
        destination = (await connection.execute(select(message_deliveries.c.recipient_work_id).where(
            message_deliveries.c.delivery_id == receipt["delivery_id"]))).scalar_one()
    assert destination == RECIPIENT_ENDPOINT
    async with engine.begin() as connection:
        await connection.execute(update(message_deliveries).where(
            message_deliveries.c.delivery_id == delivery).values(state="DISPOSITIONED"))
        await connection.execute(update(agent_mailboxes).where(
            agent_mailboxes.c.endpoint_id == target.endpoint_id).values(generation=2))
        await connection.execute(update(agent_mailboxes).where(
            agent_mailboxes.c.endpoint_id == RECIPIENT_ENDPOINT).values(generation=2))
    # A committed operation is replayable even beyond the first-observation freshness window.
    class FutureDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now + timedelta(seconds=120)
    with monkeypatch.context() as patch:
        patch.setattr("switchstand.chatgpt_alerts.datetime", FutureDateTime)
        assert await reporter.send(evidence) == receipt  # Replay before stale/cleared evidence checks.
    changed = evidence.model_copy(update={"delivery_ids": [uuid4()]})
    assert (await reporter.send(changed))["status"] == "conflict"
    async with engine.begin() as connection:
        await connection.execute(update(agent_mailboxes).where(
            agent_mailboxes.c.endpoint_id == RECIPIENT_ENDPOINT).values(generation=1))
    recovered = evidence.model_copy(update={"phase": "recovered"})
    recovery = await reporter.send(recovered)
    assert recovery["status"] == "queued"
    assert await reporter.send(recovered) == recovery
    async with engine.connect() as connection:
        rows = (await connection.execute(select(message_deliveries).where(
            message_deliveries.c.sender_work_id == sender.endpoint_id))).all()
    assert len(rows) == 3  # one test message, one incident, one recovery
    await revoke(manifest, grants)
    await revoke(manifest, grants)  # Exact revocation replay, no new version.
    assert (await reporter.send(evidence))["reason"] == "reporter_grant_not_current"
    with pytest.raises(ValueError, match="differs"):
        await bootstrap(manifest, grants, issue=True)
    await engine.dispose()
