import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.lifecycle import (
    ContinuationKind,
    LifecycleEvent,
    LifecycleRepository,
    ProfileState,
    RequiredResultPersistence,
)
from switchstand.state import PostgresState, metadata


@pytest.fixture
async def lifecycle_engine() -> AsyncEngine:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL lifecycle tests")
    if make_url(url).database != "switchstand_test":
        pytest.fail("lifecycle tests require the disposable switchstand_test database")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


async def create_subject(
    engine: AsyncEngine,
) -> tuple[RequiredResultPersistence, UUID, UUID, str]:
    work = await PostgresState(engine).bind("lifecycle-projection-test", str(uuid4()))
    token = "revision:" + str(uuid4())
    service = RequiredResultPersistence(LifecycleRepository(engine))
    obligation = await service.create(work.id, token)
    return service, obligation.obligation_id, work.id, token


async def test_result_continuation_is_identical_after_restart(
    lifecycle_engine: AsyncEngine,
) -> None:
    service, obligation_id, work_id, token = await create_subject(lifecycle_engine)
    destination, correlation = "provider:task:123:result", "result:" + str(uuid4())
    assert (await service.continuation(obligation_id, token)).kind is (
        ContinuationKind.CONTINUE_CURRENT_WORK
    )
    await service.transition(
        obligation_id,
        token,
        LifecycleEvent.RESULT_READY,
        destination_ref=destination,
        result_correlation=correlation,
    )
    before_restart = await service.continuation(obligation_id, token)

    restarted = RequiredResultPersistence(LifecycleRepository(lifecycle_engine))
    assert await restarted.continuation(obligation_id, token) == before_restart
    assert before_restart.kind is ContinuationKind.PERSIST_RESULT
    assert (before_restart.destination_ref, before_restart.result_correlation) == (
        destination,
        correlation,
    )
    stored = await restarted.repository.get(obligation_id)
    assert stored is not None and stored.work_id_ref == work_id


async def test_wrong_currentness_denies_progress_without_rebinding(
    lifecycle_engine: AsyncEngine,
) -> None:
    service, obligation_id, work_id, token = await create_subject(lifecycle_engine)
    before = await service.repository.get(obligation_id)
    denied = await service.continuation(obligation_id, "stale:" + token)
    with pytest.raises(ValueError, match="currentness|stale"):
        await service.transition(
            obligation_id,
            "stale:" + token,
            LifecycleEvent.RESULT_READY,
            destination_ref="provider:task:must-not-bind",
            result_correlation="result:must-not-bind",
        )
    assert denied.kind is ContinuationKind.RECONCILE_UNKNOWN
    assert await service.repository.get(obligation_id) == before
    assert before is not None and before.work_id_ref == work_id


async def test_ambiguous_outcome_requires_explicit_safe_reconciliation(
    lifecycle_engine: AsyncEngine,
) -> None:
    service, obligation_id, _, token = await create_subject(lifecycle_engine)
    await service.transition(
        obligation_id,
        token,
        LifecycleEvent.RESULT_READY,
        destination_ref="provider:task:456:result",
        result_correlation="result:ambiguous",
    )
    await service.transition(
        obligation_id,
        token,
        LifecycleEvent.PERSIST_OUTCOME_AMBIGUOUS,
        evidence={"detail": "provider outcome unavailable"},
    )

    restarted = RequiredResultPersistence(LifecycleRepository(lifecycle_engine))
    assert (await restarted.continuation(obligation_id, token)).kind is (
        ContinuationKind.RECONCILE_UNKNOWN
    )
    stored = await restarted.repository.get(obligation_id)
    assert stored is not None and stored.state is ProfileState.UNKNOWN
    await restarted.transition(
        obligation_id,
        token,
        LifecycleEvent.RECONCILIATION_NO_MATCH_SAFE_TO_RETRY,
    )
    assert (await restarted.continuation(obligation_id, token)).kind is (
        ContinuationKind.PERSIST_RESULT
    )


@pytest.mark.parametrize(
    "event", [LifecycleEvent.CURRENTNESS_STALE, LifecycleEvent.CURRENTNESS_UNKNOWN]
)
async def test_currentness_failure_cannot_take_persistence_retry_path(
    lifecycle_engine: AsyncEngine,
    event: LifecycleEvent,
) -> None:
    service, obligation_id, _, token = await create_subject(lifecycle_engine)
    await service.transition(
        obligation_id,
        token,
        LifecycleEvent.RESULT_READY,
        destination_ref="provider:task:stale:result",
        result_correlation="result:stale",
    )
    await service.transition(
        obligation_id,
        token,
        event,
        evidence={"observed": "binding is not current"},
    )

    assert (await service.continuation(obligation_id, token)).kind is (
        ContinuationKind.RECONCILE_UNKNOWN
    )
    with pytest.raises(ValueError, match="ambiguity|retry"):
        await service.transition(
            obligation_id,
            token,
            LifecycleEvent.RECONCILIATION_NO_MATCH_SAFE_TO_RETRY,
        )


async def test_only_exact_authoritative_readback_permits_terminal(
    lifecycle_engine: AsyncEngine,
) -> None:
    service, obligation_id, _, token = await create_subject(lifecycle_engine)
    destination = "provider:task:789:result"
    correlation = "result:" + str(uuid4())
    await service.transition(
        obligation_id,
        token,
        LifecycleEvent.RESULT_READY,
        destination_ref=destination,
        result_correlation=correlation,
    )
    for evidence in (
        {"destination_ref": destination, "result_correlation": "different-result"},
        {"destination_ref": "different-destination", "result_correlation": correlation},
    ):
        with pytest.raises(ValueError, match="destination|correlation|readback|match"):
            await service.transition(
                obligation_id,
                token,
                LifecycleEvent.PERSIST_READBACK_MATCHED,
                evidence=evidence,
            )
    assert (await service.continuation(obligation_id, token)).kind is (
        ContinuationKind.PERSIST_RESULT
    )
    await service.transition(
        obligation_id,
        token,
        LifecycleEvent.PERSIST_READBACK_MATCHED,
        evidence={"destination_ref": destination, "result_correlation": correlation},
    )
    assert (await service.continuation(obligation_id, token)).kind is ContinuationKind.TERMINAL
