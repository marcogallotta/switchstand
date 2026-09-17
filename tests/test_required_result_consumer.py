import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from chatgpt_fixture import Provider
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.chatgpt import ChatGPTService, RequiredResultSaveRequest
from switchstand.contracts import LaunchAuthority
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext, WorkGrant
from switchstand.lifecycle import (
    LifecycleEvent,
    LifecycleRepository,
    ProfileState,
    RequiredResultPersistence,
)
from switchstand.state import PostgresState, metadata


@pytest.fixture
async def result_engine() -> AsyncEngine:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for required-result integration tests")
    if make_url(url).database != "switchstand_test":
        pytest.fail("required-result tests require the disposable switchstand_test database")
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


def service_for(
    engine: AsyncEngine,
    principal: PrincipalContext,
    provider: Provider,
    required_results: RequiredResultPersistence | None = None,
) -> ChatGPTService:
    async def resolve_principal():
        return principal

    lifecycle = required_results or RequiredResultPersistence(LifecycleRepository(engine))
    return ChatGPTService(
        resolve_principal,
        PostgresState(engine),
        GrantState(engine),
        {"asana": provider},
        required_results=lifecycle,
    )


async def subject(
    engine: AsyncEngine,
    *,
    provider: Provider | None = None,
    required_results: RequiredResultPersistence | None = None,
):
    principal = PrincipalContext(
        issuer="required-result-test",
        subject=str(uuid4()),
        client_id="local-test",
        assurance="test",
    )
    state = PostgresState(engine)
    handle = await state.bind("asana", "123")
    grants = GrantState(engine)
    selected = WorkGrant(
        id=uuid4(),
        version=1,
        principal=principal,
        authority=LaunchAuthority(active_work_id=handle.id),
        operations=frozenset({"work_get", "work_append"}),
        issuer="fixture-operator",
        provenance="required-result integration",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        append_qualification="test:required-result",
    )
    await grants.issue(selected, None)
    actual_provider = provider or Provider()
    service = service_for(engine, principal, actual_provider, required_results)
    return service, principal, selected, actual_provider, service.required_results


def request(work_id, *, grant_version=1, revision="r1", text="final result"):
    return RequiredResultSaveRequest(
        api_version="1",
        work_id=work_id,
        grant_version=grant_version,
        observed_revision=revision,
        text=text,
    )


async def test_required_result_real_write_readback_closes_and_replays_without_duplicate(
    result_engine: AsyncEngine,
) -> None:
    service, _, grant, provider, lifecycle = await subject(result_engine)
    assert lifecycle is not None
    action = request(grant.authority.active_work_id)

    first = await service.required_result_save(action)
    assert first.operation_id is not None
    stored = await lifecycle.repository.get(first.operation_id)
    assert first.status == "ok" and first.effect == "applied"
    assert first.operation == "required_result_save"
    assert stored is not None and stored.state is ProfileState.TERMINAL
    assert first.receipt is not None and first.receipt.story_gid == "1"
    assert provider.sends == 1

    replay = await service.required_result_save(action)
    assert replay.status == "ok" and replay.reason == "required_result_already_persisted"
    assert replay.operation_id == first.operation_id
    assert provider.sends == 1


async def test_ambiguous_provider_send_is_durable_unknown_and_never_blindly_retried(
    result_engine: AsyncEngine,
) -> None:
    provider = Provider()
    provider.unknown = True
    service, _, grant, provider, lifecycle = await subject(result_engine, provider=provider)
    assert lifecycle is not None
    action = request(grant.authority.active_work_id)

    first = await service.required_result_save(action)
    assert first.status == "unknown" and first.effect == "unknown"
    assert first.operation == "required_result_save" and first.operation_id is not None
    stored = await lifecycle.repository.get(first.operation_id)
    assert stored is not None and stored.state is ProfileState.UNKNOWN
    assert stored.unknown_reason == LifecycleEvent.PERSIST_OUTCOME_AMBIGUOUS.value
    assert provider.sends == 1

    replay = await service.required_result_save(action)
    assert replay.status == "unknown" and replay.effect == "unknown"
    assert replay.operation_id == first.operation_id
    assert provider.sends == 1


class FailTerminalCommitOnce(RequiredResultPersistence):
    def __init__(self, repository: LifecycleRepository):
        super().__init__(repository)
        self.fail_terminal_once = True

    async def transition(self, obligation_id, currentness_token, event, **kwargs):
        if event is LifecycleEvent.PERSIST_READBACK_MATCHED and self.fail_terminal_once:
            self.fail_terminal_once = False
            raise SQLAlchemyError("injected terminal commit failure")
        return await super().transition(obligation_id, currentness_token, event, **kwargs)


async def test_crash_after_verified_write_replays_effect_after_service_restart_without_duplicate(
    result_engine: AsyncEngine,
) -> None:
    lifecycle = FailTerminalCommitOnce(LifecycleRepository(result_engine))
    service, principal, grant, provider, stored_lifecycle = await subject(
        result_engine,
        required_results=lifecycle,
    )
    assert stored_lifecycle is lifecycle
    action = request(grant.authority.active_work_id)

    interrupted = await service.required_result_save(action)
    assert interrupted.status == "unknown" and interrupted.effect == "unknown"
    assert interrupted.operation_id is not None
    assert provider.sends == 1
    stored = await lifecycle.repository.get(interrupted.operation_id)
    assert stored is not None and stored.state is ProfileState.PERSIST_REQUIRED

    restarted = service_for(result_engine, principal, provider)
    recovered = await restarted.required_result_save(action)
    assert recovered.status == "ok" and recovered.effect == "applied"
    assert recovered.operation_id == interrupted.operation_id
    assert provider.sends == 1
    assert restarted.required_results is not None
    stored = await restarted.required_results.repository.get(interrupted.operation_id)
    assert stored is not None and stored.state is ProfileState.TERMINAL


async def test_same_duty_rejects_changed_result_and_changed_grant_currentness(
    result_engine: AsyncEngine,
) -> None:
    service, principal, grant, provider, lifecycle = await subject(result_engine)
    assert lifecycle is not None
    first_request = request(
        grant.authority.active_work_id,
        revision="stale-revision",
        text="result one",
    )

    stale_source = await service.required_result_save(first_request)
    assert stale_source.status == "stale" and stale_source.operation_id is not None
    assert provider.sends == 0
    stored = await lifecycle.repository.get(stale_source.operation_id)
    assert stored is not None and stored.state is ProfileState.PERSIST_REQUIRED

    changed = request(grant.authority.active_work_id, text="different result")
    conflict = await service.required_result_save(changed)
    assert conflict.status == "denied" and conflict.reason == "lifecycle_result_identity_conflict"
    assert conflict.operation_id == stale_source.operation_id
    assert provider.sends == 0

    refreshed = WorkGrant(
        id=uuid4(),
        version=2,
        principal=principal,
        authority=grant.authority,
        operations=grant.operations,
        issuer=grant.issuer,
        provenance=grant.provenance,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        append_qualification=grant.append_qualification,
    )
    await service.grants.issue(refreshed, 1)
    current_request = request(
        grant.authority.active_work_id,
        grant_version=2,
        text="result one",
    )
    currentness = await service.required_result_save(current_request)
    assert currentness.status == "stale" and currentness.reason == "lifecycle_currentness_changed"
    assert currentness.operation_id == stale_source.operation_id
    assert provider.sends == 0
    final = await lifecycle.repository.get(stale_source.operation_id)
    assert final is not None and final.state is ProfileState.PERSIST_REQUIRED


async def test_concurrent_same_result_converges_on_one_operation_and_one_provider_write(
    result_engine: AsyncEngine,
) -> None:
    provider = Provider()
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_first_send():
        entered.set()
        await release.wait()

    provider.before_send = hold_first_send
    service, _, grant, provider, lifecycle = await subject(result_engine, provider=provider)
    assert lifecycle is not None
    action = request(grant.authority.active_work_id)

    first_task = asyncio.create_task(service.required_result_save(action))
    await entered.wait()
    second_task = asyncio.create_task(service.required_result_save(action))
    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first.status == second.status == "ok"
    assert first.operation_id is not None and second.operation_id == first.operation_id
    assert provider.sends == 1
    stored = await lifecycle.repository.get(first.operation_id)
    assert stored is not None and stored.state is ProfileState.TERMINAL
