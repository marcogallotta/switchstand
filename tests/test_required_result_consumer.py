import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from chatgpt_fixture import Provider
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.chatgpt import ChatGPTService, RequiredResultSaveRequest
from switchstand.contracts import LaunchAuthority
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext, WorkGrant
from switchstand.lifecycle import LifecycleRepository, ProfileState, RequiredResultPersistence
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


async def subject(engine: AsyncEngine):
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
    provider = Provider()
    service = service_for(engine, principal, provider)
    return service, principal, selected, provider, service.required_results


def request(work_id):
    return RequiredResultSaveRequest(
        api_version="1",
        work_id=work_id,
        grant_version=1,
        observed_revision="r1",
        text="final result",
    )


def refreshed_grant(principal: PrincipalContext, grant: WorkGrant) -> WorkGrant:
    return WorkGrant(
        id=uuid4(),
        version=grant.version + 1,
        principal=principal,
        authority=grant.authority,
        operations=grant.operations,
        issuer=grant.issuer,
        provenance=grant.provenance,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        append_qualification=grant.append_qualification,
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


async def test_grant_change_after_verified_write_cannot_terminalize_obligation(
    result_engine: AsyncEngine,
) -> None:
    service, principal, grant, provider, lifecycle = await subject(result_engine)
    assert lifecycle is not None
    action = request(grant.authority.active_work_id)
    original_append = service.gateway.append

    async def append_then_replace(current_principal, effect):
        outcome = await original_append(current_principal, effect)
        assert outcome.effect == "applied"
        await service.grants.issue(refreshed_grant(principal, grant), 1)
        return outcome

    service.gateway.append = append_then_replace
    result = await service.required_result_save(action)

    assert result.status == "stale"
    assert result.reason == "lifecycle_currentness_changed_before_terminal"
    assert result.effect == "unknown" and result.operation_id is not None
    assert provider.sends == 1
    stored = await lifecycle.repository.get(result.operation_id)
    assert stored is not None and stored.state is ProfileState.PERSIST_REQUIRED
