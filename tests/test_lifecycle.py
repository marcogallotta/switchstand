import os
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.lifecycle import LifecycleRepository, ProfileState
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


async def test_repository_roundtrip_and_restart_preserve_exact_binding(
    lifecycle_engine: AsyncEngine,
) -> None:
    work = await PostgresState(lifecycle_engine).bind("lifecycle-test", str(uuid4()))
    obligation_id, token = uuid4(), "revision:" + str(uuid4())
    repository = LifecycleRepository(lifecycle_engine)

    created = await repository.create(obligation_id, work.id, token)
    restarted = LifecycleRepository(lifecycle_engine)

    assert await restarted.get(obligation_id) == created
    assert created.work_id_ref == work.id
    assert created.currentness_token == token
    assert created.state is ProfileState.PENDING_RESULT


async def test_versioned_replace_is_atomic_and_preserves_immutable_binding(
    lifecycle_engine: AsyncEngine,
) -> None:
    work = await PostgresState(lifecycle_engine).bind("lifecycle-test", str(uuid4()))
    obligation_id = uuid4()
    repository = LifecycleRepository(lifecycle_engine)
    created = await repository.create(obligation_id, work.id, "revision:1")
    replacement = created.model_copy(
        update={
            "state": ProfileState.PERSIST_REQUIRED,
            "row_version": 2,
            "destination_ref": "provider:task:123:result",
            "result_correlation": "result:1",
        }
    )

    stored = await repository.replace(replacement, 1)
    assert stored.row_version == 2
    assert stored.work_id_ref == created.work_id_ref
    assert stored.currentness_token == created.currentness_token
    with pytest.raises(ValueError, match="stale|binding"):
        await repository.replace(replacement, 1)
