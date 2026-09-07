import asyncio
import os

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.state import PostgresState, metadata


@pytest.fixture
async def state():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL state tests")
    assert make_url(url).database == "switchstand_test", "state tests require switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    yield PostgresState(engine)
    await engine.dispose()

async def test_bind_has_stable_opaque_identity(state):
    first = await state.bind("asana", "provider-id")
    again = await state.bind("asana", "provider-id")
    assert first == again == await state.get(first.id)
    assert first.id.version == 4

async def test_lock_serializes_two_writers(state):
    handle = await state.bind("asana", "serialized")
    holding, release, attempted, entered = (asyncio.Event() for _ in range(4))

    async def first():
        async with state.locked(handle.id) as locked:
            assert locked == handle
            holding.set()
            await release.wait()

    async def second():
        await holding.wait()
        attempted.set()
        async with state.locked(handle.id):
            entered.set()

    writers = [asyncio.create_task(first()), asyncio.create_task(second())]
    await attempted.wait()
    done, _ = await asyncio.wait(writers, timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
    release.set()
    await asyncio.gather(*writers)
    assert not done and entered.is_set()
