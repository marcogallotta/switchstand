import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from switchstand import chatgpt_edge
from switchstand.canonical_relations import CanonicalRelationsRepository
from switchstand.canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_metadata,
    canonical_revision,
)
from switchstand.canonical_work_runtime import CanonicalWorkRuntime
from switchstand.chatgpt import ChatGPTService
from switchstand.contracts import LaunchAuthority, WorkSearchRequest
from switchstand.grant_state import GrantState, effect_intents, work_grants
from switchstand.grants import (
    PrincipalContext,
    ProtectedUpdate,
    ScalarPatch,
    UpdateReceipt,
    WorkGrant,
)
from switchstand.state import PostgresState, metadata


@dataclass
class Subject:
    engine: AsyncEngine
    service: ChatGPTService
    runtime: CanonicalWorkRuntime
    grants: GrantState
    principal: PrincipalContext
    work_id: UUID
    grant: WorkGrant

@pytest.fixture
async def subject(database_prerequisite: None) -> AsyncGenerator[Subject]:
    del database_prerequisite
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.run_sync(canonical_metadata.create_all)
    state, grants = PostgresState(engine), GrantState(engine)
    handle = await state.bind("fixture", str(uuid4()))
    works = CanonicalWorkRepository(engine)
    await works.create(CurrentWork(handle.id, "Database task", False, "notes"))
    principal = PrincipalContext(
        issuer="test", subject=str(uuid4()), client_id="test", assurance="test",
    )
    grant = WorkGrant(
        id=uuid4(), version=1, principal=principal,
        authority=LaunchAuthority(active_work_id=handle.id), scope="workspace",
        operations=frozenset({"work_get", "work_search", "work_update"}),
        issuer="test", provenance="disposable PostgreSQL",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        update_qualification="test:canonical",
    )
    await grants.issue(grant, None)
    runtime = CanonicalWorkRuntime(works, CanonicalRelationsRepository(engine))

    async def resolve() -> PrincipalContext:
        return principal

    service = ChatGPTService(
        resolve, state, grants, {}, canonical_work=runtime, canonical_work_active=True,
    )
    value = Subject(engine, service, runtime, grants, principal, handle.id, grant)
    yield value
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await connection.run_sync(canonical_metadata.drop_all)
    await engine.dispose()


def update(subject: Subject, operation_id: UUID | None = None, **patch: object) -> ProtectedUpdate:
    return ProtectedUpdate(
        api_version="1", operation_id=operation_id or uuid4(), work_id=subject.work_id,
        grant_version=1, observed_revision=canonical_revision(subject.work_id, 1),
        patch=ScalarPatch.model_validate(patch),
    )

async def test_service_routes_reads_and_search_to_canonical_runtime(subject: Subject) -> None:
    got = await subject.service.get(subject.work_id)
    searched = await subject.service.search(WorkSearchRequest(api_version="1", text="database"))

    assert got.status == "ok" and got.item is not None
    assert got.item.title == "Database task" and got.item.source is None
    assert searched.status == "ok" and [item.id for item in searched.items] == [subject.work_id]

async def test_atomic_update_replays_and_rejects_operation_conflict(subject: Subject) -> None:
    request = update(subject, title="Changed")

    first = await subject.service.update(request)
    replay = await subject.service.update(request)
    conflict = await subject.service.update(request.model_copy(update={
        "patch": ScalarPatch(title="Different"),
    }))

    assert first == replay
    assert first.effect == "applied" and isinstance(first.receipt, UpdateReceipt)
    assert first.receipt.provider == "postgres"
    assert first.receipt.resulting_revision == canonical_revision(subject.work_id, 2)
    assert conflict.reason == "operation_identity_conflict"
    assert (await subject.runtime.get(subject.work_id)).item.title == "Changed"  # type: ignore[union-attr]


async def test_stale_and_ungranted_updates_do_not_write_or_journal(subject: Subject) -> None:
    stale = await subject.service.update(update(subject, notes="stale").model_copy(update={
        "observed_revision": "old",
    }))
    denied = await subject.service.update(update(subject, canonical_root="forbidden"))

    assert (stale.status, stale.effect) == ("stale", "not_sent")
    assert (denied.status, denied.effect) == ("denied", "not_sent")
    async with subject.engine.connect() as connection:
        assert await connection.scalar(select(effect_intents.c.operation_id)) is None
    stored = await subject.runtime.get(subject.work_id)
    assert stored.item is not None and stored.item.notes == "notes"


async def test_effect_journal_failure_rolls_back_work_and_exact_retry_is_safe(
    subject: Subject, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = update(subject, notes="atomic")
    replace_locked = subject.runtime.works.replace_locked

    async def fail_after_write(connection: AsyncConnection, item: CurrentWork) -> None:
        await replace_locked(connection, item)
        raise SQLAlchemyError("injected journal boundary failure")

    monkeypatch.setattr(subject.runtime.works, "replace_locked", fail_after_write)
    unknown = await subject.service.update(request)
    monkeypatch.setattr(subject.runtime.works, "replace_locked", replace_locked)
    before_retry = await subject.runtime.get(subject.work_id)
    applied = await subject.service.update(request)

    assert unknown.effect == "unknown" and unknown.retry == "reconcile"
    assert before_retry.item is not None and before_retry.item.notes == "notes"
    assert applied.effect == "applied" and isinstance(applied.receipt, UpdateReceipt)
    assert applied.receipt.resulting_revision == canonical_revision(subject.work_id, 2)


async def test_canonical_runtime_is_default_off(subject: Subject) -> None:
    inactive = ChatGPTService(
        subject.service.principal, subject.service.state, subject.grants, {},
        canonical_work=subject.runtime,
    )

    assert inactive.canonical_work is subject.runtime
    assert not inactive.canonical_work_active
    assert (await inactive.get(subject.work_id)).status == "provider_error"


async def test_ordinary_admission_updates_without_persisted_grant(subject: Subject) -> None:
    async with subject.engine.begin() as connection:
        await connection.execute(delete(work_grants))
    service = ChatGPTService(
        subject.service.principal, subject.service.state, subject.grants, {},
        ordinary_workspace_admission=True, canonical_work=subject.runtime,
        canonical_work_active=True,
    )
    request = update(subject, notes="ordinary")
    applied = await service.update(request)

    assert applied.effect == "applied"
    assert await service.update(request) == applied


async def test_resource_app_preserves_canonical_runtime(
    subject: Subject, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[ChatGPTService] = []
    monkeypatch.setattr(
        chatgpt_edge, "build_ordinary_tools",
        lambda service, *_args, **_kwargs: seen.append(service) or (),
    )
    chatgpt_edge._create_resource_app(
        subject.service, issuer_url="https://issuer.example",
        resource_url="https://resource.example", auth=None,
        certification_runtime=None,
    )

    assert seen[0].canonical_work is subject.runtime
    assert seen[0].canonical_work_active
