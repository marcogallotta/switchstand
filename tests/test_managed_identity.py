import os
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.contracts import LaunchAuthority
from switchstand.grant_state import GrantState
from switchstand.managed_identity import (
    MANAGED_APPEND_QUALIFICATION,
    managed_principal,
    rotate_managed_grant,
)
from switchstand.state import metadata


@pytest.fixture
async def engine() -> AsyncEngine:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for managed identity tests")
    if make_url(url).database != "switchstand_test":
        pytest.fail("managed identity tests require switchstand_test")
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


async def test_managed_identity_is_stable_across_replacement_and_grant_rotates(engine):
    active, reference = uuid4(), uuid4()
    grants = GrantState(engine)
    first_authority = LaunchAuthority(
        active_work_id=active,
        reference_work_ids=(reference,),
    )
    first_principal, first = await rotate_managed_grant(grants, first_authority)

    assert first_principal == managed_principal(active)
    assert first.principal == first_principal
    assert first.version == 1 and first.current()
    assert first.scope == "launch"
    assert first.operations == frozenset({"work_get", "work_append"})
    assert first.append_qualification == MANAGED_APPEND_QUALIFICATION
    assert await grants.current(first_principal.key) == first

    next_reference = uuid4()
    second_principal, second = await rotate_managed_grant(
        grants,
        LaunchAuthority(active_work_id=active, reference_work_ids=(next_reference,)),
    )
    assert second_principal == first_principal
    assert second_principal.key == first_principal.key
    assert second.id != first.id and second.version == 2
    assert second.authority.active_work_id == active
    assert second.authority.reference_work_ids == (next_reference,)
    assert await grants.current(first_principal.key) == second


async def test_different_work_gets_different_managed_principal(engine):
    grants = GrantState(engine)
    one, two = uuid4(), uuid4()
    first_principal, first = await rotate_managed_grant(
        grants, LaunchAuthority(active_work_id=one)
    )
    second_principal, second = await rotate_managed_grant(
        grants, LaunchAuthority(active_work_id=two)
    )
    assert first_principal.key != second_principal.key
    assert first.authority.active_work_id == one
    assert second.authority.active_work_id == two
    assert first.version == second.version == 1
