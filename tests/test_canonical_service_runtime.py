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
from switchstand.canonical_event_reads import CanonicalEventReader
from switchstand.canonical_relations import (
    CanonicalRelationsRepository,
    project_memberships,
    projects,
    work_dependencies,
    work_parents,
)
from switchstand.canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_metadata,
    canonical_revision,
)
from switchstand.canonical_work_runtime import CanonicalWorkRuntime
from switchstand.chatgpt import ChatGPTService
from switchstand.contracts import (
    LaunchAuthority,
    WorkEventRequest,
    WorkGetRequest,
    WorkHistoryRequest,
    WorkResolveReferenceRequest,
    WorkSearchRequest,
)
from switchstand.grant_state import GrantState, effect_intents, work_grants
from switchstand.grants import (
    CreateReceipt,
    EffectReceipt,
    PrincipalContext,
    ProtectedAppend,
    ProtectedCreate,
    ProtectedRelation,
    ProtectedUpdate,
    RelationPatch,
    RelationReceipt,
    ScalarPatch,
    UpdateReceipt,
    WorkGrant,
)
from switchstand.mcp import controller_from_env
from switchstand.state import PostgresState, metadata, work_handles
from switchstand.work_events import WorkEventRepository


@dataclass
class Subject:
    engine: AsyncEngine
    service: ChatGPTService
    runtime: CanonicalWorkRuntime
    events: WorkEventRepository
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
    handle = await state.bind("asana", "123")
    works = CanonicalWorkRepository(engine)
    await works.create(CurrentWork(handle.id, "Database task", False, "notes"))
    principal = PrincipalContext(
        issuer="test", subject=str(uuid4()), client_id="test", assurance="test",
    )
    grant = WorkGrant(
        id=uuid4(), version=1, principal=principal,
        authority=LaunchAuthority(active_work_id=handle.id), scope="workspace",
        operations=frozenset({
            "work_get", "work_search", "work_append", "work_create", "work_update",
            "work_relate",
        }),
        issuer="test", provenance="disposable PostgreSQL",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        update_qualification="test:canonical",
        append_qualification="test:canonical",
        create_qualification="test:canonical",
        relation_qualification="test:canonical",
    )
    await grants.issue(grant, None)
    runtime = CanonicalWorkRuntime(works, CanonicalRelationsRepository(engine))
    events = WorkEventRepository(engine)
    event_reader = CanonicalEventReader(works, events)

    async def resolve() -> PrincipalContext:
        return principal

    service = ChatGPTService(
        resolve, state, grants, {}, canonical_work=runtime,
        canonical_events=event_reader, canonical_work_active=True,
    )
    value = Subject(engine, service, runtime, events, grants, principal, handle.id, grant)
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


def create(subject: Subject, operation_id: UUID | None = None, **values: object) -> ProtectedCreate:
    return ProtectedCreate.model_validate({
        "api_version": "1", "operation_id": operation_id or uuid4(), "grant_version": 1,
        "title": "Created", "notes": "created notes", "parent_work_id": subject.work_id,
    } | values)


def relation(
    subject: Subject, patch: RelationPatch, operation_id: UUID | None = None,
    observed_version: int = 1,
) -> ProtectedRelation:
    return ProtectedRelation(
        api_version="1", operation_id=operation_id or uuid4(), work_id=subject.work_id,
        grant_version=1, observed_revision=canonical_revision(
            subject.work_id, observed_version
        ), patch=patch,
    )

async def test_service_routes_reads_and_search_to_canonical_runtime(subject: Subject) -> None:
    got = await subject.service.get(subject.work_id)
    searched = await subject.service.search(WorkSearchRequest(api_version="1", text="database"))

    assert got.status == "ok" and got.item is not None
    assert got.item.title == "Database task" and got.item.source is None
    assert searched.status == "ok" and [item.id for item in searched.items] == [subject.work_id]


async def test_managed_controller_constructs_db_only_canonical_reads(
    subject: Subject, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference, unbound = uuid4(), uuid4()
    works = CanonicalWorkRepository(subject.engine)
    await works.create(CurrentWork(reference, "Reference", False, "reference notes"))
    await works.create(CurrentWork(unbound, "Unbound", False, "unbound notes"))
    unbound_event = await subject.events.append(
        unbound, observed_version=1, operation_id=uuid4(), subtype="comment",
        text="private", created_at=datetime.now(UTC), actor="Marco",
    )
    monkeypatch.setenv("ACTIVE_WORK_ID", str(subject.work_id))
    monkeypatch.setenv("REFERENCE_WORK_IDS", str(reference))
    monkeypatch.setenv("DATABASE_URL", subject.engine.url.render_as_string(hide_password=False))
    monkeypatch.delenv("ASANA_TOKEN", raising=False)
    controller = controller_from_env()

    result = await controller.get(WorkGetRequest(
        api_version="1", work_id=subject.work_id,
    ))

    assert result.status == "ok" and result.item is not None
    assert result.item.title == "Database task"
    assert (await controller.get(WorkGetRequest(
        api_version="1", work_id=reference,
    ))).status == "ok"
    revision = canonical_revision(unbound, 2)
    assert (await controller.get(WorkGetRequest(
        api_version="1", work_id=unbound,
    ))).status == "denied"
    assert (await controller.history(WorkHistoryRequest(
        api_version="1", work_id=unbound, observed_revision=revision,
    ))).status == "denied"
    assert (await controller.event(WorkEventRequest(
        api_version="1", work_id=unbound, event_id=unbound_event.event.id,
        observed_revision=revision,
    ))).status == "denied"
    assert controller.providers == {}
    await controller.state.engine.dispose()  # type: ignore[attr-defined]


async def test_service_routes_reference_and_event_reads_to_postgres(subject: Subject) -> None:
    resolved = await subject.service.resolve_reference(WorkResolveReferenceRequest(
        api_version="1", reference="123",
    ))
    assert resolved.status == "ok" and resolved.item is not None
    assert resolved.item.id == subject.work_id

    created = datetime.now(UTC)
    appended = await subject.events.append(
        subject.work_id, observed_version=1, operation_id=uuid4(), subtype="comment",
        text="database history", created_at=created, actor="Marco",
    )
    revision = canonical_revision(subject.work_id, 2)
    history = await subject.service.history(WorkHistoryRequest(
        api_version="1", work_id=subject.work_id, observed_revision=revision,
    ))
    event = await subject.service.event(WorkEventRequest(
        api_version="1", work_id=subject.work_id, event_id=appended.event.id,
        observed_revision=revision,
    ))

    assert history.status == "ok" and history.events[0].id == appended.event.id
    assert event.status == "ok" and event.item is not None
    assert event.item.text == "database history"


async def test_canonical_reference_does_not_discover_missing_provider_work(
    subject: Subject,
) -> None:
    missing = await subject.service.resolve_reference(WorkResolveReferenceRequest(
        api_version="1", reference="999",
    ))

    assert missing.status == "unknown"

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


async def test_atomic_create_replays_and_persists_parent_without_provider(
    subject: Subject,
) -> None:
    request = create(subject)

    first = await subject.service.create(request)
    replay = await subject.service.create(request)
    conflict = await subject.service.create(request.model_copy(update={"title": "Different"}))

    assert first == replay and first.effect == "applied"
    assert isinstance(first.receipt, CreateReceipt)
    assert first.receipt.provider == "postgres" and first.receipt.task_gid == str(first.work_id)
    assert conflict.reason == "operation_identity_conflict"
    stored = await subject.runtime.get(first.work_id)  # type: ignore[arg-type]
    assert stored.status == "ok" and stored.item is not None
    assert stored.item.title == "Created" and stored.item.notes == "created notes"
    async with subject.engine.connect() as connection:
        assert await connection.scalar(select(work_parents.c.parent_work_id)) == subject.work_id
        handle = (await connection.execute(select(work_handles).where(
            work_handles.c.id == first.work_id
        ))).mappings().one()
        assert (handle["provider"], handle["provider_work_id"]) == (
            "postgres", str(first.work_id),
        )


async def test_active_service_routes_append_to_canonical_events(subject: Subject) -> None:
    request = ProtectedAppend(
        api_version="1", operation_id=uuid4(), work_id=subject.work_id,
        grant_version=subject.grant.version,
        observed_revision=canonical_revision(subject.work_id, 1), text="database append",
    )

    applied = await subject.service.append(request)

    assert applied.effect == "applied" and isinstance(applied.receipt, EffectReceipt)
    assert applied.receipt.provider == "postgres"
    assert await subject.service.append(request) == applied


async def test_workspace_create_uses_existing_project_alias(subject: Subject) -> None:
    project_id = uuid4()
    async with subject.engine.begin() as connection:
        await connection.execute(projects.insert().values(
            project_id=project_id, asana_project_gid="999", name="Switchstand",
        ))
    request = create(subject, parent_work_id=None, project_gid="999")

    applied = await subject.service.create(request)
    missing = await subject.service.create(create(
        subject, parent_work_id=None, project_gid="998",
    ))

    assert applied.effect == "applied" and applied.receipt.project_gid == "999"
    assert missing.reason == "project_not_admitted" and missing.effect == "not_sent"
    async with subject.engine.connect() as connection:
        membership = (await connection.execute(select(
            project_memberships.c.project_id, project_memberships.c.work_id
        ))).one()
        assert membership == (project_id, applied.work_id)


async def test_authenticated_create_requires_real_qualification(subject: Subject) -> None:
    principal = subject.principal.model_copy(update={
        "subject": str(uuid4()), "assurance": "authenticated",
    })
    grant = subject.grant.model_copy(update={
        "id": uuid4(), "principal": principal, "create_qualification": "real:canonical",
    })
    await subject.grants.issue(grant, None)

    async def resolve() -> PrincipalContext:
        return principal

    service = ChatGPTService(
        resolve, subject.service.state, subject.grants, {}, canonical_work=subject.runtime,
        canonical_events=subject.service.canonical_events, canonical_work_active=True,
    )
    assert (await service.create(create(subject))).effect == "applied"
    for version, qualification in enumerate(("test:create", "legacy:create", "garbage"), 2):
        grant = grant.model_copy(update={
            "id": uuid4(), "version": version, "create_qualification": qualification,
        })
        await subject.grants.issue(grant, version - 1)
        denied = await service.create(create(subject, grant_version=version))
        assert denied.reason == "create_not_qualified_for_this_surface"


async def test_create_journal_failure_rolls_back_row_and_exact_retry_is_safe(
    subject: Subject, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = create(subject)
    create_locked = subject.runtime.works.create_locked

    async def fail_after_write(connection: AsyncConnection, item: CurrentWork) -> None:
        await create_locked(connection, item)
        raise SQLAlchemyError("injected journal boundary failure")

    monkeypatch.setattr(subject.runtime.works, "create_locked", fail_after_write)
    unknown = await subject.service.create(request)
    monkeypatch.setattr(subject.runtime.works, "create_locked", create_locked)
    before_retry = await subject.runtime.get(unknown.work_id)  # type: ignore[arg-type]
    applied = await subject.service.create(request)

    assert unknown.effect == "unknown" and unknown.retry == "reconcile"
    assert before_retry.status == "unknown"
    assert applied.effect == "applied" and isinstance(applied.receipt, CreateReceipt)


async def test_atomic_parent_relation_replays_and_rejects_conflict(subject: Subject) -> None:
    target = uuid4()
    await subject.runtime.works.create(CurrentWork(target, "Parent", False, ""))
    request = relation(subject, RelationPatch(
        kind="parent", action="set", target_work_id=target,
    ))

    first = await subject.service.relate(request)
    replay = await subject.service.relate(request)
    conflict = await subject.service.relate(request.model_copy(update={
        "patch": RelationPatch(kind="parent", action="clear"),
    }))

    assert first == replay and first.effect == "applied"
    assert isinstance(first.receipt, RelationReceipt)
    assert first.receipt.provider == "postgres"
    assert conflict.reason == "operation_identity_conflict"
    stored = await subject.runtime.relations.get(subject.work_id)
    assert stored.parent_work_id == target
    assert (await subject.runtime.works.get(subject.work_id)).row_version == 2  # type: ignore[union-attr]


async def test_atomic_dependency_relation_and_unsupported_kind(subject: Subject) -> None:
    target = uuid4()
    await subject.runtime.works.create(CurrentWork(target, "Dependency", False, ""))

    applied = await subject.service.relate(relation(subject, RelationPatch(
        kind="dependency", action="add", target_work_id=target,
    )))
    unsupported = await subject.service.relate(relation(
        subject, RelationPatch(kind="assignee", action="set", assignee_gid="123"),
        observed_version=2,
    ))

    assert applied.effect == "applied"
    assert unsupported.reason == "invalid_database_relation"
    async with subject.engine.connect() as connection:
        assert (await connection.execute(select(
            work_dependencies.c.work_id, work_dependencies.c.depends_on_work_id
        ))).one() == (subject.work_id, target)


async def test_relation_journal_failure_rolls_back_change_and_retry_is_safe(
    subject: Subject, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = uuid4()
    await subject.runtime.works.create(CurrentWork(target, "Parent", False, ""))
    request = relation(subject, RelationPatch(
        kind="parent", action="set", target_work_id=target,
    ))
    set_parent = subject.runtime.relations.set_parent

    async def fail_after_write(
        work_id: UUID, parent_work_id: UUID | None, observed_version: int, *,
        connection: AsyncConnection | None = None,
    ) -> None:
        await set_parent(
            work_id, parent_work_id, observed_version, connection=connection
        )
        raise SQLAlchemyError("injected journal boundary failure")

    monkeypatch.setattr(subject.runtime.relations, "set_parent", fail_after_write)
    unknown = await subject.service.relate(request)
    monkeypatch.setattr(subject.runtime.relations, "set_parent", set_parent)
    before_retry = await subject.runtime.relations.get(subject.work_id)
    applied = await subject.service.relate(request)

    assert unknown.effect == "unknown" and unknown.retry == "reconcile"
    assert before_retry.parent_work_id is None
    assert applied.effect == "applied" and isinstance(applied.receipt, RelationReceipt)


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
        canonical_work=subject.runtime, canonical_events=subject.service.canonical_events,
    )

    assert inactive.canonical_work is subject.runtime
    assert not inactive.canonical_work_active
    assert (await inactive.get(subject.work_id)).status == "provider_error"


async def test_active_canonical_reads_fail_closed_when_repository_is_missing(
    subject: Subject,
) -> None:
    missing = ChatGPTService(
        subject.service.principal, subject.service.state, subject.grants, {},
        canonical_work_active=True,
    )
    revision = canonical_revision(subject.work_id, 1)

    assert (await missing.get(subject.work_id)).status == "unknown"
    assert (await missing.search(WorkSearchRequest(api_version="1"))).status == "unknown"
    assert (await missing.resolve_reference(WorkResolveReferenceRequest(
        api_version="1", reference="123",
    ))).status == "unknown"
    assert (await missing.history(WorkHistoryRequest(
        api_version="1", work_id=subject.work_id, observed_revision=revision,
    ))).status == "unknown"
    assert (await missing.event(WorkEventRequest(
        api_version="1", work_id=subject.work_id, event_id=uuid4(),
        observed_revision=revision,
    ))).status == "unknown"


async def test_ordinary_admission_updates_without_persisted_grant(subject: Subject) -> None:
    async with subject.engine.begin() as connection:
        await connection.execute(delete(work_grants))
    service = ChatGPTService(
        subject.service.principal, subject.service.state, subject.grants, {},
        ordinary_workspace_admission=True, canonical_work=subject.runtime,
        canonical_events=subject.service.canonical_events,
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
    assert seen[0].canonical_events is subject.service.canonical_events
    assert seen[0].canonical_work_active
