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
from switchstand.effects import AppendGateway
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
    yield engine
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
    await engine.dispose()


async def subject(
    engine: AsyncEngine,
    lifecycle: RequiredResultPersistence | None = None,
) -> tuple[ChatGPTService, PrincipalContext, WorkGrant, Provider]:
    principal = PrincipalContext(
        issuer="required-result-test", subject=str(uuid4()),
        client_id="local-test", assurance="test",
    )
    state, grants = PostgresState(engine), GrantState(engine)
    handle = await state.bind("asana", "123")
    grant = WorkGrant(
        id=uuid4(), version=1, principal=principal,
        authority=LaunchAuthority(active_work_id=handle.id),
        operations=frozenset({"work_get", "work_append"}),
        issuer="fixture-operator", provenance="required-result integration",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        append_qualification="test:required-result",
    )
    await grants.issue(grant, None)
    provider = Provider()

    async def resolve() -> PrincipalContext:
        return principal

    service = ChatGPTService(
        resolve, state, grants, {"asana": provider},
        required_results=lifecycle or RequiredResultPersistence(LifecycleRepository(engine)),
    )
    return service, principal, grant, provider


def request(grant: WorkGrant, text: str = "final result") -> RequiredResultSaveRequest:
    return RequiredResultSaveRequest(
        api_version="1", work_id=grant.authority.active_work_id,
        grant_version=grant.version, observed_revision="r1", text=text,
    )


async def test_save_closes_and_restart_replays_without_duplicate(
    result_engine: AsyncEngine,
) -> None:
    service, principal, grant, provider = await subject(result_engine)
    action = request(grant)

    first = await service.required_result_save(action)
    assert first.status == "ok" and first.effect == "applied"
    assert first.operation_id is not None and provider.sends == 1
    stored = await service.required_results.repository.get(first.operation_id)  # type: ignore[union-attr]
    assert stored is not None and stored.state is ProfileState.TERMINAL

    async def resolve() -> PrincipalContext:
        return principal

    restarted = ChatGPTService(
        resolve, PostgresState(result_engine), GrantState(result_engine), {"asana": provider},
        required_results=RequiredResultPersistence(LifecycleRepository(result_engine)),
    )
    replay = await restarted.required_result_save(action)
    assert replay.status == "ok" and replay.reason == "required_result_already_persisted"
    assert replay.operation_id == first.operation_id and provider.sends == 1

    conflict = await restarted.required_result_save(request(grant, "different result"))
    assert conflict.status == "denied"
    assert conflict.reason == "lifecycle_result_identity_conflict" and provider.sends == 1


async def test_terminal_replay_survives_authorized_grant_renewal_without_duplicate(
    result_engine: AsyncEngine,
) -> None:
    service, _, grant, provider = await subject(result_engine)
    first = await service.required_result_save(request(grant))
    assert first.status == "ok" and first.effect == "applied" and provider.sends == 1

    renewed = grant.model_copy(update={"id": uuid4(), "version": 2})
    await service.grants.issue(renewed, 1)
    replay = await service.required_result_save(request(renewed))

    assert replay.status == "ok" and replay.reason == "required_result_already_persisted"
    assert replay.operation_id == first.operation_id and provider.sends == 1


class FailTerminalOnce(RequiredResultPersistence):
    async def transition(self, obligation_id, currentness_token, event, **kwargs):
        if event is LifecycleEvent.PERSIST_READBACK_MATCHED:
            raise SQLAlchemyError("injected terminal commit failure")
        return await super().transition(obligation_id, currentness_token, event, **kwargs)


async def test_terminal_commit_interruption_recovers_through_existing_effect_record(
    result_engine: AsyncEngine,
) -> None:
    service, principal, grant, provider = await subject(
        result_engine, FailTerminalOnce(LifecycleRepository(result_engine))
    )
    action = request(grant)
    interrupted = await service.required_result_save(action)
    assert interrupted.status == "unknown" and interrupted.effect == "unknown"
    assert interrupted.operation_id is not None and provider.sends == 1

    async def resolve() -> PrincipalContext:
        return principal

    restarted = ChatGPTService(
        resolve, PostgresState(result_engine), GrantState(result_engine), {"asana": provider},
        required_results=RequiredResultPersistence(LifecycleRepository(result_engine)),
    )
    recovered = await restarted.required_result_save(action)
    assert recovered.status == "ok" and recovered.effect == "applied"
    assert recovered.operation_id == interrupted.operation_id and provider.sends == 1
    stored = await restarted.required_results.repository.get(recovered.operation_id)  # type: ignore[union-attr]
    assert stored is not None and stored.state is ProfileState.TERMINAL


class RenewAfterReady(RequiredResultPersistence):
    def __init__(self, repository, grants, renewed):
        super().__init__(repository)
        self.grants, self.renewed = grants, renewed

    async def transition(self, obligation_id, currentness_token, event, **kwargs):
        result = await super().transition(obligation_id, currentness_token, event, **kwargs)
        if event is LifecycleEvent.RESULT_READY:
            await self.grants.issue(self.renewed, 1)
        return result


async def test_no_send_stale_obligation_adopts_renewed_current_grant(
    result_engine: AsyncEngine,
) -> None:
    service, _, grant, provider = await subject(result_engine)
    renewed = grant.model_copy(update={"id": uuid4(), "version": 2})
    service.required_results = RenewAfterReady(
        LifecycleRepository(result_engine), service.grants, renewed
    )

    stale = await service.required_result_save(request(grant))
    assert stale.status == "stale" and stale.effect == "not_sent" and provider.sends == 0

    recovered = await service.required_result_save(request(renewed))
    assert recovered.status == "ok" and recovered.effect == "applied" and provider.sends == 1
    stored = await service.required_results.repository.get(recovered.operation_id)  # type: ignore[union-attr]
    assert stored is not None and stored.state is ProfileState.TERMINAL


class RenewAfterApplied(AppendGateway):
    def __init__(self, state, grants, providers, renewed):
        super().__init__(state, grants, providers)
        self.renewed = renewed

    async def append(self, principal, request):
        outcome = await super().append(principal, request)
        if outcome.effect == "applied":
            await self.grants.issue(self.renewed, 1)
        return outcome


async def test_applied_readback_terminalizes_after_grant_renewal_without_second_write(
    result_engine: AsyncEngine,
) -> None:
    service, _, grant, provider = await subject(result_engine)
    renewed = grant.model_copy(update={"id": uuid4(), "version": 2})
    service.gateway = RenewAfterApplied(service.state, service.grants, service.providers, renewed)

    interrupted = await service.required_result_save(request(grant))
    assert interrupted.status == "stale" and interrupted.effect == "unknown"
    assert provider.sends == 1

    recovered = await service.required_result_save(request(renewed))
    assert recovered.status == "ok" and recovered.effect == "applied"
    assert recovered.operation_id == interrupted.operation_id and provider.sends == 1
    stored = await service.required_results.repository.get(recovered.operation_id)  # type: ignore[union-attr]
    assert stored is not None and stored.state is ProfileState.TERMINAL
