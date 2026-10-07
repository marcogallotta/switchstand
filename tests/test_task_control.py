import os
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_metadata,
    canonical_revision,
)
from switchstand.contracts import LaunchAuthority
from switchstand.grants import PrincipalContext, WorkGrant
from switchstand.task_control import DurableControlCapsule, TaskControlState


@pytest.fixture
async def subject(
    database_prerequisite: None,
) -> AsyncGenerator[tuple[AsyncEngine, CanonicalWorkRepository, TaskControlState, UUID,
                           PrincipalContext, WorkGrant]]:
    del database_prerequisite
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(canonical_metadata.create_all)
    works, work_id = CanonicalWorkRepository(engine), uuid4()
    await works.create(CurrentWork(
        work_id, "Controlled", False, "initial notes",
        priority="HIGH", work_type="Task", lifecycle_state="CURRENT",
        canonical_root=str(work_id), owner_key="agent:root",
        wait_kind="NONE", unblock_condition="NONE", next_due="NONE",
        next_action_class="OWNER_CAN_DO", next_action_ref="implement",
    ))
    principal = PrincipalContext(
        issuer="test", subject=str(uuid4()), client_id="test", assurance="test",
    )
    grant = WorkGrant(
        id=uuid4(), version=1, principal=principal,
        authority=LaunchAuthority(active_work_id=work_id), scope="launch",
        operations=frozenset({"work_get", "task_control"}), issuer="test",
        provenance="test", expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    yield engine, works, TaskControlState(engine, works), work_id, principal, grant
    async with engine.begin() as connection:
        await connection.run_sync(canonical_metadata.drop_all)
    await engine.dispose()


def capsule(**changes: object) -> DurableControlCapsule:
    values = {
        "objective": "Deliver the exact approved outcome",
        "completion_condition": "All acceptance checks pass",
        "success_proof": "Exact-head test receipt",
        "target_refs": ("work:subject",),
        "intended_effect_class": "repository implementation",
        "progress_summary": "Implementation started",
        "progress_evidence_ref": "git:base",
    }
    values.update(changes)
    return DurableControlCapsule.model_validate(values)


async def test_checkpoint_replay_cas_and_selective_currentness(subject) -> None:
    engine, works, state, work_id, principal, grant = subject
    del engine
    operation_id = uuid4()
    revision = canonical_revision(work_id, 1)
    first = await state.checkpoint(
        principal, grant, operation_id, work_id, revision, None, capsule(),
    )
    replay = await state.checkpoint(
        principal, grant, operation_id, work_id, revision, None, capsule(),
    )
    conflict = await state.checkpoint(
        principal, grant, operation_id, work_id, revision, None,
        capsule(progress_summary="different"),
    )
    assert first.status == "ok" and first.currentness == "CURRENT"
    assert replay.receipt == first.receipt and replay.reason == "exact_replay"
    assert conflict.status == "conflict"

    current = await works.get(work_id)
    assert current is not None
    await works.replace(replace(current, notes="progress only"))
    notes_only = await state.read(work_id)
    assert notes_only.currentness == "CURRENT"

    current = await works.get(work_id)
    assert current is not None
    await works.replace(replace(current, priority="LOW"))
    changed = await state.read(work_id)
    assert changed.currentness == "STALE" and changed.reason == "control_basis_changed"
    stale_replay = await state.checkpoint(
        principal, grant, operation_id, work_id, revision, None, capsule(),
    )
    assert stale_replay.receipt == first.receipt
    assert stale_replay.currentness == "STALE"
    assert stale_replay.reason == "replayed_checkpoint_stale"


async def test_checkpoint_rejects_stale_generation_revision_and_authority(subject) -> None:
    engine, works, state, work_id, principal, grant = subject
    del engine, works
    stale_revision = await state.checkpoint(
        principal, grant, uuid4(), work_id, "stale", None, capsule(),
    )
    assert stale_revision.status == "stale"

    revision = canonical_revision(work_id, 1)
    first = await state.checkpoint(
        principal, grant, uuid4(), work_id, revision, None, capsule(),
    )
    stale_generation = await state.checkpoint(
        principal, grant, uuid4(), work_id, revision, None, capsule(),
    )
    assert first.status == "ok" and stale_generation.reason == "checkpoint_generation_changed"

    read_only = grant.model_copy(update={"operations": frozenset({"work_get"})})
    denied = await state.checkpoint(
        principal, read_only, uuid4(), work_id, revision, 1, capsule(),
    )
    assert denied.status == "denied"
