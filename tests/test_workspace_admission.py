import os
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.grant_state import GrantState
from switchstand.grants import GuardOutcome, PrincipalContext
from switchstand.state import PostgresState, metadata
from switchstand.workspace_admission import WorkspaceAdmissionState


@pytest.mark.asyncio
async def test_workspace_admission_is_stable_without_work_grant_and_preserves_effect_journal():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.run_sync(metadata.create_all)
    try:
        principal = PrincipalContext(
            issuer="fixture", subject=str(uuid4()), client_id="ordinary",
            assurance="authenticated",
        )

        async def resolve():
            return principal

        raw = GrantState(engine)
        admission = WorkspaceAdmissionState(engine, resolve)
        state = PostgresState(engine)
        handle = await state.bind("asana", "123")

        assert await raw.current(principal.key) is None
        first = await admission.current(principal.key)
        second = await admission.current(principal.key)
        assert first is not None and first == second
        assert first.scope == "workspace" and first.version == 1
        assert first.can_write(handle.id)
        assert first.create_qualification == "real:ordinary-workspace"

        operation_id = uuid4()
        unknown = GuardOutcome(
            status="unknown", operation="work_append", work_id=handle.id,
            operation_id=operation_id, reason="prepared", effect="unknown",
            retry="reconcile", next_action="Reconcile.",
        )
        async with admission.locked(principal.key, handle.id) as locked:
            assert locked == first
            await admission.prepare({"proof": True}, locked, "fingerprint", unknown)
        record = await raw.exact(operation_id)
        assert record is not None
        assert record.principal_key == principal.key
        assert (record.grant_id, record.grant_version) == (first.id, first.version)
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()
