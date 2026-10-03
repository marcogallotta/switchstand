import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from switchstand.canonical_work import (
    canonical_metadata,
    canonical_revision,
    canonical_work,
)
from switchstand.contracts import LaunchAuthority
from switchstand.effects import CanonicalAppendGateway
from switchstand.grant_state import GrantState, effect_intents, work_grants
from switchstand.grants import (
    EffectReceipt,
    GuardOutcome,
    PrincipalContext,
    ProtectedAppend,
    WorkGrant,
)
from switchstand.state import metadata
from switchstand.work_events import WorkEventRepository, work_events


@dataclass
class Subject:
    engine: AsyncEngine
    gateway: CanonicalAppendGateway
    events: WorkEventRepository
    principal: PrincipalContext
    grant: WorkGrant
    work_id: UUID


@pytest.fixture
async def subject(database_prerequisite: None) -> AsyncGenerator[Subject]:
    del database_prerequisite
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.run_sync(canonical_metadata.create_all)
    work_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(insert(canonical_work).values(
            work_id=work_id, title="Append target", normalized_title="append target",
            completed=False, notes="", row_version=1,
        ))
    principal = PrincipalContext(
        issuer="test", subject=str(uuid4()), client_id="test", assurance="test",
    )
    grant = WorkGrant(
        id=uuid4(), version=1, principal=principal,
        authority=LaunchAuthority(active_work_id=work_id),
        scope="workspace",
        operations=frozenset({"work_append"}), issuer="test", provenance="test",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        append_qualification="test:canonical",
    )
    grants = GrantState(engine)
    await grants.issue(grant, None)
    events = WorkEventRepository(engine)
    yield Subject(engine, CanonicalAppendGateway(grants, events), events, principal, grant, work_id)
    async with engine.begin() as connection:
        await connection.execute(delete(effect_intents).where(
            effect_intents.c.principal_key == principal.key
        ))
        await connection.execute(delete(work_grants).where(
            work_grants.c.principal_key == principal.key
        ))
        await connection.execute(delete(work_events).where(work_events.c.work_id == work_id))
        await connection.execute(delete(canonical_work).where(canonical_work.c.work_id == work_id))
    await engine.dispose()


def request(subject: Subject, operation_id: UUID | None = None, text: str = "result") -> ProtectedAppend:
    return ProtectedAppend(
        api_version="1", operation_id=operation_id or uuid4(), work_id=subject.work_id,
        grant_version=subject.grant.version,
        observed_revision=canonical_revision(subject.work_id, 1), text=text,
    )


async def test_append_event_revision_and_receipt_commit_and_replay(subject: Subject) -> None:
    value = request(subject)
    applied = await subject.gateway.append(subject.principal, value)
    replay = await CanonicalAppendGateway(
        GrantState(subject.engine), WorkEventRepository(subject.engine)
    ).append(subject.principal, value)

    assert replay == applied and applied.effect == "applied"
    assert isinstance(applied.receipt, EffectReceipt)
    assert applied.receipt.provider == "postgres"
    assert applied.receipt.task_gid == str(subject.work_id)
    async with subject.engine.connect() as connection:
        assert await connection.scalar(select(canonical_work.c.row_version).where(
            canonical_work.c.work_id == subject.work_id
        )) == 2
        assert await connection.scalar(select(func.count()).select_from(work_events).where(
            work_events.c.work_id == subject.work_id
        )) == 1
        assert await connection.scalar(select(func.count()).select_from(effect_intents).where(
            effect_intents.c.operation_id == str(value.operation_id)
        )) == 1


async def test_append_rejects_stale_terminal_and_operation_conflict(subject: Subject) -> None:
    value = request(subject)
    assert (await subject.gateway.append(
        subject.principal, value.model_copy(update={"observed_revision": "stale"})
    )).status == "stale"
    applied = await subject.gateway.append(subject.principal, value)
    conflict = await subject.gateway.append(
        subject.principal, value.model_copy(update={"text": "different"})
    )
    assert applied.effect == "applied" and conflict.reason == "operation_identity_conflict"

    other_id = uuid4()
    async with subject.engine.begin() as connection:
        await connection.execute(insert(canonical_work).values(
            work_id=other_id, title="Done", normalized_title="done",
            completed=True, notes="", row_version=1,
        ))
    terminal = request(subject).model_copy(update={"work_id": other_id})
    assert (await subject.gateway.append(subject.principal, terminal)).reason == "work_is_terminal"
    async with subject.engine.begin() as connection:
        await connection.execute(delete(canonical_work).where(canonical_work.c.work_id == other_id))


async def test_append_rolls_back_event_revision_and_receipt_on_failure(
    subject: Subject, monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = request(subject)
    append_locked = subject.events.append_locked

    async def fail_after_append(connection: AsyncConnection, *args: object, **kwargs: object):
        await append_locked(connection, *args, **kwargs)  # type: ignore[arg-type]
        raise SQLAlchemyError("journal boundary")

    monkeypatch.setattr(subject.events, "append_locked", fail_after_append)
    unknown = await subject.gateway.append(subject.principal, value)
    monkeypatch.setattr(subject.events, "append_locked", append_locked)

    assert unknown.effect == "unknown"
    async with subject.engine.connect() as connection:
        assert await connection.scalar(select(canonical_work.c.row_version).where(
            canonical_work.c.work_id == subject.work_id
        )) == 1
        assert await connection.scalar(select(func.count()).select_from(work_events).where(
            work_events.c.work_id == subject.work_id
        )) == 0
        assert await connection.scalar(select(func.count()).select_from(effect_intents).where(
            effect_intents.c.operation_id == str(value.operation_id)
        )) == 0
    assert (await subject.gateway.append(subject.principal, value)).effect == "applied"


async def test_append_preserves_existing_unknown_effect_barrier(subject: Subject) -> None:
    blocked_id = uuid4()
    blocked = GuardOutcome(
        status="unknown", operation="work_update", work_id=subject.work_id,
        operation_id=blocked_id, reason="provider_unknown", effect="unknown",
        retry="reconcile", next_action="reconcile",
    )
    async with subject.engine.begin() as connection:
        await connection.execute(insert(effect_intents).values(
            operation_id=str(blocked_id), fingerprint="old",
            principal_key=subject.principal.key, work_id=str(subject.work_id),
            grant_id=str(subject.grant.id), grant_version=subject.grant.version,
            intent={"authority": "provider"}, outcome=blocked.model_dump(mode="json"),
        ))

    result = await subject.gateway.append(subject.principal, request(subject))
    assert result.reason == "target_has_unresolved_effect"
    assert result.blocked_by is not None and result.blocked_by.operation_id == blocked_id
    async with subject.engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(work_events).where(
            work_events.c.work_id == subject.work_id
        )) == 0
