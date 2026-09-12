import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from chatgpt_fixture import PRINCIPAL, Provider, grant
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.chatgpt import ChatGPTService
from switchstand.contracts import (
    LaunchAuthority,
    SourceStoriesRequest,
    SourceStoryRequest,
    SourceTaskRequest,
)
from switchstand.core import ProviderError
from switchstand.effects import AppendGateway
from switchstand.grant_state import GrantState, effect_intents
from switchstand.grants import ProtectedAppend
from switchstand.state import PostgresState, metadata


@pytest.fixture
async def subject():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL grant/effect tests")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    state, grants = PostgresState(engine), GrantState(engine)
    provider_name = "fixture-" + str(uuid4())
    active, reference = await state.bind(provider_name, "123"), await state.bind(provider_name, "456")
    principal = PRINCIPAL.model_copy(update={"subject": str(uuid4())})
    selected = grant(principal=principal, active=active.id, reference=reference.id)
    await grants.issue(selected, None)
    async def resolve():
        return principal
    provider = Provider()
    service = ChatGPTService(resolve, state, grants, {provider_name: provider, "asana": provider})
    yield service, selected, provider
    await engine.dispose()


def request(selected, **changes):
    return ProtectedAppend(**({'api_version': "1", 'operation_id': uuid4(), 'work_id': selected.authority.active_work_id, 'grant_version': selected.version, 'observed_revision': "r1", 'text': "reply to exact source 123/story 1"} | changes))


async def test_trusted_issuance_is_versioned_and_replacement_removes_old_work(subject):
    service, selected, provider = subject
    with pytest.raises(ValueError, match="stale"):
        await service.grants.issue(selected, None)
    with pytest.raises(ValueError, match="exactly once"):
        await service.grants.issue(selected.model_copy(update={"version": 3}), 1)
    old_request = request(selected)
    replacement = selected.model_copy(update={"version": 2, "id": uuid4(), "authority":
        LaunchAuthority(active_work_id=selected.authority.reference_work_ids[0])})
    await service.grants.issue(replacement, 1)
    restarted = GrantState(service.grants.engine)
    assert await restarted.current(selected.principal.key) == replacement
    assert (await service.append(old_request)).status == "denied"
    assert (await service.get()).item.id == replacement.authority.active_work_id
    assert provider.sends == 0


@pytest.mark.parametrize("changes", [
    {"state": "revoked"}, {"state": "terminal"},
    {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
    {"operations": frozenset({"work_get"})}, {"append_qualification": None},
    {"append_qualification": "real:unqualified-test-principal"},
])
async def test_current_grant_restrictions_deny_before_send(subject, changes):
    service, selected, provider = subject
    revised = selected.model_copy(update=changes | {"version": 2})
    await service.grants.issue(revised, 1)
    result = await service.append(request(revised))
    assert result.status == "denied" and result.effect == "not_sent"
    assert provider.sends == 0


async def test_wrong_work_revision_and_grant_version_never_send(subject):
    service, selected, provider = subject
    for changes, status in [({"work_id": uuid4()}, "denied"),
        ({"work_id": selected.authority.reference_work_ids[0]}, "denied"),
        ({"observed_revision": "old"}, "stale"), ({"grant_version": 2}, "stale")]:
        assert (await service.append(request(selected, **changes))).status == status
    wrong = selected.principal.model_copy(update={"subject": "stranger"})
    assert (await service.gateway.append(wrong, request(selected))).status == "denied"
    assert provider.sends == 0


async def test_exact_operation_replay_and_distinct_later_same_text(subject):
    service, selected, provider = subject
    req = request(selected)
    first = await service.append(req)
    assert first.status == "ok" and first.effect == "applied"
    assert first.receipt.principal == selected.principal
    assert first.receipt.grant_id == selected.id and first.receipt.grant_version == 1
    assert first.receipt.work_id == req.work_id and first.receipt.task_gid == "123"
    assert first.receipt.story_gid == "1" and first.receipt.text == req.text
    restarted = AppendGateway(service.state, GrantState(service.grants.engine), service.providers)
    assert await restarted.append(selected.principal, req) == first
    assert (await restarted.append(selected.principal,
        req.model_copy(update={"operation_id": uuid4()}))).status == "stale"
    conflict = await service.append(req.model_copy(update={"text": "different payload"}))
    assert conflict.status == "denied" and conflict.reason == "operation_identity_conflict"
    assert provider.sends == 1
    later = req.model_copy(update={"operation_id": uuid4(), "observed_revision": provider.revision})
    second = await restarted.append(selected.principal, later)
    assert second.status == "ok" and second.operation_id == later.operation_id
    assert second.receipt.story_gid == "2" and second.receipt.text == first.receipt.text
    assert provider.sends == 2


async def test_definite_nonapplication_does_not_suppress_authorized_recovery(subject, monkeypatch):
    service, selected, provider = subject
    req = request(selected)
    async def reject(*args):
        raise ProviderError("definite rejection before effect")
    with monkeypatch.context() as patch:
        patch.setattr(provider, "append", reject)
        rejected = await service.append(req)
    assert rejected.status == "not_applied" and rejected.effect == "not_sent"
    assert await service.append(req) == rejected
    recovered = await service.append(req.model_copy(update={"operation_id": uuid4()}))
    assert recovered.status == "ok" and recovered.receipt.text == req.text
    assert provider.sends == 1


@pytest.mark.parametrize("prior_unknown", [False, True])
@pytest.mark.parametrize("failure_at", ["lock", "lookup"])
async def test_unreadable_history_preserves_prior_effect_uncertainty(subject, monkeypatch, prior_unknown, failure_at):
    service, selected, provider = subject
    provider.unknown = prior_unknown
    req = request(selected)
    first = await service.append(req)
    assert first.effect == ("unknown" if prior_unknown else "applied")
    async def unavailable(*args):
        raise SQLAlchemyError("journal unavailable on reentry")
    @asynccontextmanager
    async def unavailable_lock(*args):
        await unavailable()
        yield None
    with monkeypatch.context() as patch:
        patch.setattr(service.grants, "locked" if failure_at == "lock" else "previous",
                      unavailable_lock if failure_at == "lock" else unavailable)
        replay = await service.append(req)
    assert replay.status == "unknown" and replay.effect == "unknown"
    assert replay.retry == "reconcile" and "do not send a new operation" in replay.next_action
    assert replay.receipt is None and provider.sends == 1
    assert await service.append(req) == first
    assert provider.sends == 1


@pytest.mark.parametrize("failure", ["unknown", "mismatch", "cancel", "finish"])
async def test_ambiguous_send_blocks_new_id_changed_payload_and_other_principal(subject, failure, monkeypatch):
    service, selected, provider = subject
    req = request(selected)
    if failure == "finish":
        async def unavailable(outcome):
            raise SQLAlchemyError("lost receipt storage")
        monkeypatch.setattr(service.grants, "finish", unavailable)
    else:
        setattr(provider, failure, True)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await service.append(req)
    else:
        first = await service.append(req)
        assert first.status == "unknown" and first.effect == "unknown" and first.receipt is None
    restarted = GrantState(service.grants.engine)
    gateway = AppendGateway(service.state, restarted, service.providers)
    for retry in (req, request(selected, text="changed payload", observed_revision=provider.revision)):
        result = await gateway.append(selected.principal, retry)
        assert result.status == "unknown" and result.retry == "reconcile"
    another = selected.principal.model_copy(update={"subject": str(uuid4())})
    other_grant = selected.model_copy(update={"principal": another, "id": uuid4()})
    await restarted.issue(other_grant, None)
    result = await gateway.append(another, request(other_grant, text="another caller",
                                                   observed_revision=provider.revision))
    assert result.status == "unknown" and provider.sends == 1


async def test_intent_is_committed_before_send_and_two_callers_send_once(subject):
    service, selected, provider = subject
    req, entered, release = request(selected), asyncio.Event(), asyncio.Event()
    async def inspect_intent():
        async with service.grants.engine.connect() as connection:
            row = (await connection.execute(select(effect_intents).where(
                effect_intents.c.operation_id == str(req.operation_id)))).mappings().one()
        assert row["intent"]["request"]["text"] == req.text
        assert row["outcome"]["effect"] == "unknown"
        entered.set()
        await release.wait()
    provider.before_send = inspect_intent
    first = asyncio.create_task(service.append(req))
    await asyncio.wait_for(entered.wait(), 5)
    second = asyncio.create_task(service.append(req.model_copy(update={"operation_id": uuid4()})))
    done, _ = await asyncio.wait([first, second], timeout=0.1)
    release.set()
    results = await asyncio.gather(first, second)
    assert not done and results[0].status == "ok" and results[1].status == "stale"
    assert provider.sends == 1


async def test_revocation_serializes_with_inflight_send_then_denies(subject):
    service, selected, provider = subject
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        entered.set()
        await release.wait()
    provider.before_send = hold
    send = asyncio.create_task(service.append(request(selected)))
    await asyncio.wait_for(entered.wait(), 5)
    revoked = selected.model_copy(update={"version": 2, "state": "revoked"})
    revoke = asyncio.create_task(service.grants.issue(revoked, 1))
    done, _ = await asyncio.wait([revoke], timeout=0.1)
    release.set()
    assert (await send).status == "ok"
    await revoke
    assert not done
    assert (await service.append(request(revoked, text="later"))).status == "denied"
    assert provider.sends == 1


async def test_inbox_arrival_reread_receipt_and_reentry(subject):
    service, selected, provider = subject
    source = SourceTaskRequest(api_version="1", task_gid="123")
    assert (await service.source_task(source)).item.notes == "initial notes"
    await provider.append("123", "sender=coordinator; request inspect source 123/story 1")
    provider.notes = "updated message in notes"
    assert (await service.source_task(source)).item.notes == "updated message in notes"
    page = SourceStoriesRequest(api_version="1", task_gid="123", observed_revision="r1", limit=1)
    assert (await service.source_stories(page)).status == "stale"
    page = page.model_copy(update={"observed_revision": provider.revision})
    message = (await service.source_stories(page)).stories[0]
    reread = await service.source_story(SourceStoryRequest(api_version="1", task_gid="123",
        story_gid=message.story_gid, observed_revision=provider.revision))
    assert reread.item == message and message.created_by == "shared-provider-author"
    req = request(selected, observed_revision=provider.revision)
    receipt = await service.append(req)
    assert receipt.status == "ok" and receipt.receipt.principal == selected.principal
    service.grants = GrantState(service.grants.engine)
    service.gateway = AppendGateway(service.state, service.grants, service.providers)
    assert await service.append(req) == receipt and provider.sends == 2
    current = (await service.source_task(source)).item.revision
    first_page = await service.source_stories(page.model_copy(update={"observed_revision": current}))
    assert first_page.next_offset == "1"
    second = await service.source_stories(page.model_copy(update={
        "observed_revision": current, "offset": first_page.next_offset}))
    assert second.stories[0].story_gid == receipt.receipt.story_gid and second.next_offset is None
