import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Table, delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from switchstand import canonical_work_runtime, chatgpt_edge
from switchstand.agent_mailboxes import AgentMailbox, AgentMailboxState
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
from switchstand.canonical_work_runtime import (
    CONFIRM_CANCELS_REVIEW_MESSAGE,
    CanonicalWorkRuntime,
)
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
from switchstand.messages import MessageReceiveRequest, MessageState, RuntimeCurrentness
from switchstand.reviews import (
    ReviewFinding,
    ReviewGuidelines,
    ReviewOccurrenceState,
    ReviewPolicy,
    ReviewRequest,
    ReviewService,
    ReviewSubmit,
)
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
    await works.create(CurrentWork(
        handle.id, "Database task", False, "notes",
        priority="UNSET", work_type="Task", lifecycle_state="CURRENT",
        canonical_root=str(handle.id), owner_key="agent:root",
        wait_kind="NONE", unblock_condition="NONE", next_due="NONE",
        next_action_class="OWNER_CAN_DO", next_action_ref="implement",
    ))
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
        await connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, activation_obligation_revisions"
        ))
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
        "work_type": "Task", "lifecycle_state": "CURRENT", "owner_key": "agent:root",
        "wait_kind": "NONE", "unblock_condition": "NONE", "next_due": "NONE",
        "next_action_class": "OWNER_CAN_DO", "next_action_ref": "implement",
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


async def pending_review(
    subject: Subject,
) -> tuple[
    UUID, UUID, ReviewService, AgentMailbox, AgentMailbox, MessageState,
]:
    mailboxes = AgentMailboxState(subject.engine)
    requester = (await mailboxes.register_agent(
        "Requester", "principal-requester", "chat-requester",
    )).mailbox
    reviewer = (await mailboxes.register_agent(
        "Coordinator", "principal-reviewer", "chat-reviewer",
    )).mailbox
    assert requester is not None and reviewer is not None
    policy = ReviewPolicy(version="policy-v1")
    occurrences = ReviewOccurrenceState(subject.runtime.works, mailboxes, policy)
    messages = MessageState(subject.engine, subject.grants)
    reviews = ReviewService(
        occurrences, mailboxes, messages, policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    requested = await reviews.request(ReviewRequest(
        subject_work_id=subject.work_id,
        observed_revision=canonical_revision(subject.work_id, 1),
        review_kind="CODE",
    ), requester)
    assert requested.status == "SENT"
    assert requested.review_id is not None and requested.delivery_id is not None
    return (
        requested.review_id, requested.delivery_id, reviews,
        requester, reviewer, messages,
    )


async def guarded_write(
    subject: Subject, kind: str, operation_id: UUID, *, confirm: bool = False,
    observed_version: int = 1,
):
    revision = canonical_revision(subject.work_id, observed_version)
    if kind == "update":
        return await subject.service.update(ProtectedUpdate(
            api_version="1", operation_id=operation_id, work_id=subject.work_id,
            grant_version=subject.grant.version, observed_revision=revision,
            patch=ScalarPatch(notes="confirmed edit"),
            confirm_cancels_review=confirm,
        ))
    return await subject.service.append(ProtectedAppend(
        api_version="1", operation_id=operation_id, work_id=subject.work_id,
        grant_version=subject.grant.version, observed_revision=revision,
        text="confirmed append", confirm_cancels_review=confirm,
    ))

async def test_service_routes_reads_and_search_to_canonical_runtime(subject: Subject) -> None:
    got = await subject.service.get(subject.work_id)
    searched = await subject.service.search(WorkSearchRequest(api_version="1", text="database"))

    assert got.status == "ok" and got.item is not None
    assert got.item.title == "Database task" and got.item.source is None
    assert searched.status == "ok" and [item.id for item in searched.items] == [subject.work_id]


async def test_service_updates_and_projects_coherent_lifecycle_routing(
    subject: Subject,
) -> None:
    incomplete = await subject.service.update(update(
        subject, completed=True, wait_kind="DEPENDENCY",
    ))
    changed = await subject.service.update(update(
        subject, lifecycle_state="WAITING", wait_kind="DEPENDENCY",
        unblock_condition="dependency completes", next_due="2026-10-05",
        canonical_root=str(subject.work_id), owner_key="agent:root",
        next_action_class="NONE", next_action_ref="NONE",
    ))
    readback = await subject.service.get(subject.work_id)

    assert (incomplete.effect, incomplete.reason) == (
        "not_sent", "invalid_resultant_state",
    )
    assert changed.effect == "applied"
    assert readback.status == "ok" and readback.item is not None
    assert {
        "lifecycle_state": readback.item.routing.lifecycle_state,
        "wait_kind": readback.item.routing.wait_kind,
        "unblock_condition": readback.item.routing.unblock_condition,
        "next_due": readback.item.routing.next_due,
    } == {
        "lifecycle_state": "WAITING",
        "wait_kind": "DEPENDENCY",
        "unblock_condition": "dependency completes",
        "next_due": "2026-10-05",
    }


async def test_terminal_work_412ad158_closes_in_disposable_replica(
    subject: Subject,
) -> None:
    work_id = UUID("412ad158-90e8-5754-94b0-81612be92a35")
    await subject.runtime.works.create(CurrentWork(
        work_id, "P0 CORRECTION — observability V1 misses reviewer-acquisition waits",
        False, "Behavioral correction complete; only administrative cleanup remains",
        priority="P0", work_type="Correction", lifecycle_state="TERMINAL",
        canonical_root="UNKNOWN", owner_key="UNKNOWN", wait_kind="NONE",
        unblock_condition="NONE", next_due="NONE", next_action_class="NONE",
        next_action_ref="NONE",
    ))
    replica = replace(subject, work_id=work_id)

    closed = await subject.service.update(update(replica, completed=True))
    readback = await subject.runtime.get(work_id)

    assert closed.effect == "applied"
    assert readback.status == "ok" and readback.item is not None
    assert readback.item.completed is True
    assert readback.item.routing.lifecycle_state == "TERMINAL"


async def test_updates_and_inherited_creates_reject_unbound_exact_root(
    subject: Subject,
) -> None:
    missing = uuid4()
    denied = await subject.service.update(update(
        subject, lifecycle_state="CURRENT", canonical_root=str(missing),
        owner_key="agent:root", wait_kind="NONE", unblock_condition="NONE",
        next_due="NONE", next_action_class="NONE", next_action_ref="NONE",
    ))
    current = await subject.runtime.works.get(subject.work_id)
    assert current is not None and current.row_version == 1
    await subject.runtime.works.replace(replace(current, canonical_root=str(missing)))
    inherited = await subject.service.create(create(subject))

    assert denied.reason == inherited.reason == "canonical_root_not_bound"


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


async def test_atomic_update_round_trips_supported_routing_and_preserves_fences(
    subject: Subject,
) -> None:
    root = str(subject.work_id)
    request = update(
        subject,
        lifecycle_state="CURRENT",
        canonical_root=root,
        owner_key="coordinator",
        wait_kind="NONE",
        unblock_condition="NONE",
        next_due="NONE",
        next_action_class="OWNER_CAN_DO",
        next_action_ref="implement",
    )

    applied = await subject.service.update(request)
    replay = await subject.service.update(request)
    stale = await subject.service.update(update(
        subject, operation_id=uuid4(), priority="P1",
    ))
    readback = await subject.service.get(subject.work_id)

    assert applied == replay
    assert applied.effect == "applied" and isinstance(applied.receipt, UpdateReceipt)
    assert applied.receipt.resulting_revision == canonical_revision(subject.work_id, 2)
    assert (stale.status, stale.effect, stale.reason) == (
        "stale", "not_sent", "source_revision_changed",
    )
    assert readback.status == "ok" and readback.item is not None
    assert readback.item.revision == canonical_revision(subject.work_id, 2)
    routing = readback.item.routing.model_dump()
    assert {
        field: routing[field]
        for field in (
            "canonical_root", "owner_key", "next_action_class", "next_action_ref"
        )
    } == {
        "canonical_root": root,
        "owner_key": "coordinator",
        "next_action_class": "OWNER_CAN_DO",
        "next_action_ref": "implement",
    }
    async with subject.engine.connect() as connection:
        assert len((await connection.execute(select(effect_intents))).all()) == 1


async def test_atomic_create_replays_and_persists_parent_without_provider(
    subject: Subject,
) -> None:
    request = create(subject)

    first = await subject.service.create(request)
    replay = await subject.service.create(request)
    conflict = await subject.service.create(request.model_copy(update={"title": "Different"}))
    root_conflict = await subject.service.create(create(subject, canonical_root="NONE"))

    assert first == replay and first.effect == "applied"
    assert isinstance(first.receipt, CreateReceipt)
    assert first.receipt.provider == "postgres" and first.receipt.task_gid == str(first.work_id)
    assert conflict.reason == "operation_identity_conflict"
    assert root_conflict.reason == "child_root_conflicts_with_parent"
    stored = await subject.runtime.get(first.work_id)  # type: ignore[arg-type]
    assert stored.status == "ok" and stored.item is not None
    assert stored.item.title == "Created" and stored.item.notes == "created notes"
    assert stored.item.routing.canonical_root == str(subject.work_id)
    assert stored.item.routing.owner_key == "agent:root"
    assert stored.item.routing.next_action_class == "OWNER_CAN_DO"
    async with subject.engine.connect() as connection:
        assert await connection.scalar(select(work_parents.c.parent_work_id)) == subject.work_id
        handle = (await connection.execute(select(work_handles).where(
            work_handles.c.id == first.work_id
        ))).mappings().one()
        assert (handle["provider"], handle["provider_work_id"]) == (
            "postgres", str(first.work_id),
        )


async def test_navigation_parent_starts_exact_child_root(subject: Subject) -> None:
    async with subject.engine.begin() as connection:
        await connection.execute(work_handles.update().where(
            work_handles.c.id == subject.work_id
        ).values(provider_work_id="1218327002478382"))
    current = await subject.runtime.works.get(subject.work_id)
    assert current is not None
    await subject.runtime.works.replace(replace(
        current, work_type="Context", lifecycle_state=None, canonical_root=None,
        owner_key=None, wait_kind=None, unblock_condition=None, next_due=None,
        next_action_class=None, next_action_ref=None,
    ))

    applied = await subject.service.create(create(subject))

    assert applied.status == "ok" and applied.effect == "applied"
    stored = await subject.runtime.get(applied.work_id)  # type: ignore[arg-type]
    assert stored.item is not None
    assert stored.item.routing.canonical_root == str(applied.work_id)
    async with subject.engine.connect() as connection:
        assert await connection.scalar(select(work_parents.c.parent_work_id)) == subject.work_id


async def test_arbitrary_rootless_context_cannot_start_child_root(subject: Subject) -> None:
    current = await subject.runtime.works.get(subject.work_id)
    assert current is not None
    await subject.runtime.works.replace(replace(
        current, work_type="Context", lifecycle_state=None, canonical_root=None,
        owner_key=None, wait_kind=None, unblock_condition=None, next_due=None,
        next_action_class=None, next_action_ref=None,
    ))

    denied = await subject.service.create(create(subject))

    assert (denied.status, denied.effect, denied.reason) == (
        "denied", "not_sent", "parent_root_not_exact",
    )


async def test_start_here_rejects_non_direct_assignment_child_shapes(
    subject: Subject,
) -> None:
    async with subject.engine.begin() as connection:
        await connection.execute(work_handles.update().where(
            work_handles.c.id == subject.work_id
        ).values(provider_work_id="1218327002478382"))
    current = await subject.runtime.works.get(subject.work_id)
    assert current is not None
    await subject.runtime.works.replace(replace(
        current, work_type="Context", lifecycle_state=None, canonical_root=None,
        owner_key=None, wait_kind=None, unblock_condition=None, next_due=None,
        next_action_class=None, next_action_ref=None,
    ))
    shapes = (
        {"work_type": "Implementation"},
        {
            "lifecycle_state": "WAITING", "wait_kind": "REVIEW",
            "unblock_condition": "REVIEW_OUTCOME", "next_due": "UNSCHEDULED",
            "next_action_class": "NONE", "next_action_ref": "NONE",
        },
        {"owner_key": "NONE"},
        {"wait_kind": "UNKNOWN"},
        {"next_action_class": "NONE", "next_action_ref": "NONE"},
    )

    for values in shapes:
        denied = await subject.service.create(create(subject, **values))
        assert (denied.status, denied.effect, denied.reason) == (
            "denied", "not_sent", "parent_root_not_exact",
        )


async def test_start_here_malformed_or_nonexact_root_fails_closed(subject: Subject) -> None:
    async with subject.engine.begin() as connection:
        await connection.execute(work_handles.update().where(
            work_handles.c.id == subject.work_id
        ).values(provider_work_id="1218327002478382"))
    current = await subject.runtime.works.get(subject.work_id)
    assert current is not None

    for invalid_root in ("not-a-work-id", f"{{{subject.work_id}}}"):
        current = await subject.runtime.works.replace(replace(
            current, work_type="Context", lifecycle_state=None,
            canonical_root=invalid_root, owner_key=None, wait_kind=None,
            unblock_condition=None, next_due=None, next_action_class=None,
            next_action_ref=None,
        ))
        denied = await subject.service.create(create(subject))
        assert (denied.status, denied.effect, denied.reason) == (
            "denied", "not_sent", "parent_root_not_exact",
        )


async def test_rootless_non_navigation_parent_and_legacy_minimal_create_fail_closed(
    subject: Subject,
) -> None:
    current = await subject.runtime.works.get(subject.work_id)
    assert current is not None
    await subject.runtime.works.replace(replace(current, canonical_root=None))
    ordinary = await subject.service.create(create(subject))

    context = await subject.runtime.works.get(subject.work_id)
    assert context is not None
    await subject.runtime.works.replace(replace(context, work_type="Context"))
    explicit = await subject.service.create(create(
        subject, canonical_root=str(uuid4()),
    ))
    minimal = await subject.service.create(ProtectedCreate(
        api_version="1", operation_id=uuid4(), grant_version=1,
        title="Legacy minimal", parent_work_id=subject.work_id,
    ))

    assert ordinary.reason == "parent_root_not_exact"
    assert explicit.reason == "parent_root_not_exact"
    assert minimal.reason == "parent_root_not_exact"


async def test_create_classifies_invalid_resultant_state_and_accepts_coherent_current(
    subject: Subject,
) -> None:
    invalid = await subject.service.create(create(
        subject,
        lifecycle_state="CURRENT",
        wait_kind="NONE",
        unblock_condition="NONE",
        next_action_class="UNKNOWN",
        next_action_ref="UNKNOWN",
    ))
    coherent = await subject.service.create(create(
        subject,
        lifecycle_state="CURRENT",
        wait_kind="NONE",
        unblock_condition="NONE",
        next_due="NONE",
        next_action_class="OWNER_CAN_DO",
        next_action_ref="implement",
    ))

    assert (invalid.status, invalid.effect, invalid.reason) == (
        "denied", "not_sent", "invalid_resultant_state",
    )
    assert coherent.effect == "applied"


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


@pytest.mark.parametrize("kind", ["update", "append"])
async def test_pending_review_requires_confirmation_without_writing(
    subject: Subject, kind: str,
) -> None:
    review_id, *_rest = await pending_review(subject)
    operation_id = uuid4()

    warned = await guarded_write(subject, kind, operation_id)

    assert (warned.status, warned.effect, warned.reason, warned.next_action) == (
        "CONFIRM_REQUIRED", "not_sent", "pending_review",
        CONFIRM_CANCELS_REVIEW_MESSAGE.format(review_id=review_id),
    )
    stored = await subject.runtime.works.get(subject.work_id)
    assert stored is not None and stored.row_version == 1 and stored.notes == "notes"
    async with subject.engine.connect() as connection:
        assert await connection.scalar(select(effect_intents.c.operation_id).where(
            effect_intents.c.operation_id == str(operation_id)
        )) is None
        assert await connection.scalar(select(subject.events.events.c.id).where(
            subject.events.events.c.operation_id == operation_id
        )) is None


@pytest.mark.parametrize("kind", ["update", "append"])
async def test_confirmed_pending_review_write_applies_and_stales_review(
    subject: Subject, kind: str,
) -> None:
    review_id, _delivery_id, reviews, requester, _reviewer, _messages = (
        await pending_review(subject)
    )
    operation_id = uuid4()
    warned = await guarded_write(subject, kind, operation_id)

    applied = await guarded_write(subject, kind, operation_id, confirm=True)

    assert warned.status == "CONFIRM_REQUIRED"
    assert applied.status == "ok" and applied.effect == "applied"
    stored = await subject.runtime.works.get(subject.work_id)
    assert stored is not None and stored.row_version == 2
    stale = await reviews.get(review_id, requester)
    assert (stale.status, stale.reason) == ("STALE", "review_basis_changed")
    async with subject.engine.connect() as connection:
        assert await connection.scalar(select(effect_intents.c.operation_id).where(
            effect_intents.c.operation_id == str(operation_id)
        )) == str(operation_id)


async def test_saved_stale_findings_do_not_trigger_spurious_confirmation(
    subject: Subject,
) -> None:
    review_id, delivery_id, reviews, _requester, reviewer, messages = (
        await pending_review(subject)
    )
    received = await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(generation="1", current_generation="1"),
        MessageReceiveRequest(
            api_version="1", delivery_id=delivery_id,
            grant_version=reviewer.generation,
        ),
        agent_binding=reviewer,
    )
    assert received.status == "ok"
    first = await guarded_write(subject, "update", uuid4(), confirm=True)
    assert first.status == "ok"
    finding = ReviewFinding(
        finding_id="F-late", defect="Missing proof", evidence="No boundary result",
        consequence="Claim is unsafe", affected_claim="landing",
        minimum_clearing_condition="Supply boundary proof",
    )
    stale = await reviews.submit(ReviewSubmit(
        review_id=review_id, verdict="FINDINGS", findings=(finding,),
        context_provenance="UNSEEDED",
    ), reviewer)
    assert (stale.status, stale.reason) == ("STALE", "subject_revision_changed")

    second = await guarded_write(
        subject, "update", uuid4(), observed_version=2,
    )

    assert second.status == "ok" and second.effect == "applied"
    stored = await subject.runtime.works.get(subject.work_id)
    assert stored is not None and stored.row_version == 3


@pytest.mark.parametrize("kind", ["update", "append"])
async def test_write_without_pending_review_is_unchanged(
    subject: Subject, kind: str,
) -> None:
    applied = await guarded_write(subject, kind, uuid4())

    assert applied.status == "ok" and applied.effect == "applied"


async def test_workspace_create_uses_existing_project_alias(subject: Subject) -> None:
    project_id = uuid4()
    async with subject.engine.begin() as connection:
        await connection.execute(projects.insert().values(
            project_id=project_id, asana_project_gid="999", name="Switchstand",
        ))
    request = create(subject, parent_work_id=None, project_id=project_id)

    applied = await subject.service.create(request)
    replay = await subject.service.create(request)
    conflicting_root = await subject.service.create(create(
        subject, parent_work_id=None, project_id=project_id,
        canonical_root=str(subject.work_id),
    ))
    missing = await subject.service.create(create(
        subject, parent_work_id=None, project_gid="998",
    ))
    missing_root = await subject.service.create(create(
        subject, parent_work_id=None, project_id=project_id, canonical_root=str(uuid4()),
    ))

    assert applied == replay and applied.effect == "applied"
    assert conflicting_root.reason == "root_must_be_self"
    assert missing.reason == "project_not_admitted" and missing.effect == "not_sent"
    assert missing_root.reason == "root_must_be_self"
    created = await subject.runtime.get(applied.work_id)  # type: ignore[arg-type]
    assert created.item is not None
    assert created.item.routing.canonical_root == str(applied.work_id)
    async with subject.engine.connect() as connection:
        memberships = (await connection.execute(select(
            project_memberships.c.project_id, project_memberships.c.work_id
        ))).all()
        assert set(memberships) == {(project_id, applied.work_id)}


@pytest.mark.parametrize("category", ["BUG", "PERFORMANCE"])
async def test_issue_report_is_ownerless_atomic_and_exactly_replayable(
    subject: Subject, category: str,
) -> None:
    project_id = uuid4()
    async with subject.engine.begin() as connection:
        await connection.execute(projects.insert().values(
            project_id=project_id, name=category,
        ))
    subject.service.issue_projects[category] = project_id
    subject.grant = subject.grant.model_copy(update={
        "version": 2,
        "scope": "launch",
        "operations": subject.grant.operations | {"file_issue"},
        "report_qualification": "test:issue-report",
    })
    await subject.grants.issue(subject.grant, 1)
    operation_id = uuid4()

    applied = await subject.service.file_issue(category, operation_id, "Broken", "raw evidence")
    replay = await subject.service.file_issue(category, operation_id, "Broken", "raw evidence")
    conflict = await subject.service.file_issue(category, operation_id, "Changed", "raw evidence")

    assert applied == replay and applied.effect == "applied"
    assert applied.suggestion_status == "OK" and not applied.candidates
    assert "context-free fork" in applied.code_reconciliation_instruction
    assert "run in parallel where possible" in applied.code_reconciliation_instruction
    assert conflict.reason == "operation_identity_conflict"
    stored = await subject.runtime.get(applied.work_id)  # type: ignore[arg-type]
    assert stored.item is not None
    assert stored.item.notes == "raw evidence"
    assert stored.item.routing.work_type == category
    assert stored.item.routing.owner_key == "NONE"
    assert stored.item.routing.lifecycle_state == "WAITING"
    async with subject.engine.connect() as connection:
        membership = await connection.scalar(select(project_memberships.c.project_id).where(
            project_memberships.c.work_id == applied.work_id
        ))
    assert membership == project_id


@pytest.mark.parametrize(("classification", "closed"), [
    ("NEW", False), ("AMEND", True), ("DUPLICATE", True),
    ("SUB-ITEM", False), ("RELATED", False),
])
async def test_feedback_classification_is_atomic_exact_and_replayable(
    subject: Subject, monkeypatch: pytest.MonkeyPatch, classification: str, closed: bool,
) -> None:
    category, project_id = "BUG", uuid4()
    async with subject.engine.begin() as connection:
        await connection.execute(projects.insert().values(project_id=project_id, name=category))
    subject.service.issue_projects[category] = project_id
    subject.grant = subject.grant.model_copy(update={
        "version": 2, "operations": subject.grant.operations | {"file_issue"},
        "report_qualification": "test:feedback",
    })
    await subject.grants.issue(subject.grant, 1)
    target = await subject.service.file_issue(
        category, uuid4(), "Shared crash target", "target evidence",
    )
    await subject.service.file_issue(category, uuid4(), "unshared", "decoy evidence")
    source = await subject.service.file_issue(
        category, uuid4(), "Shared crash source", "source evidence",
    )
    assert target.work_id is not None and source.work_id is not None
    assert [candidate.id for candidate in source.candidates] == [target.work_id]
    operation_id = uuid4()
    target_fields = {} if classification == "NEW" else {
        "target_work_id": target.work_id,
        "observed_target_revision": canonical_revision(target.work_id, 1),
    }
    arguments = {
        "mode": "CLASSIFY", "source_work_id": source.work_id,
        "observed_source_revision": canonical_revision(source.work_id, 1),
        "classification": classification, **target_fields,
    }
    if classification in {"AMEND", "SUB-ITEM"}:
        def fail_journal(_table: Table):
            raise SQLAlchemyError("injected classification failure")

        failure_id = uuid4()
        with monkeypatch.context() as failure_patch:
            failure_patch.setattr(canonical_work_runtime, "insert", fail_journal)
            failed = await subject.service.file_issue(category, failure_id, **arguments)  # type: ignore[arg-type]
        rolled_source = await subject.runtime.works.get(source.work_id)
        rolled_target = await subject.runtime.works.get(target.work_id)
        assert failed.effect == "unknown" and rolled_source is not None and rolled_target is not None
        assert rolled_source.notes == "source evidence" and rolled_target.notes == "target evidence"
        assert (await subject.runtime.relations.get(source.work_id)).parent_work_id is None
        async with subject.engine.connect() as connection:
            assert await connection.scalar(select(effect_intents.c.operation_id).where(
                effect_intents.c.operation_id == str(failure_id)
            )) is None
    applied = await subject.service.file_issue(category, operation_id, **arguments)  # type: ignore[arg-type]
    replay = await subject.service.file_issue(category, operation_id, **arguments)  # type: ignore[arg-type]
    assert applied == replay and applied.effect == "applied"
    changed = (await subject.runtime.get(source.work_id)).item
    assert changed is not None and changed.completed is closed
    assert f"Classification: {classification}" in changed.notes
    if classification == "AMEND":
        amended = (await subject.runtime.get(target.work_id)).item
        assert amended is not None and str(source.work_id) in amended.notes
    if classification == "SUB-ITEM":
        assert (await subject.runtime.relations.get(source.work_id)).parent_work_id == target.work_id
    if classification == "NEW":
        unclassified = await subject.service.file_issue(category, uuid4(), "Source", "evidence")
        assert unclassified.work_id is not None
        stale = await subject.service.file_issue(
            category, uuid4(), mode="CLASSIFY", source_work_id=unclassified.work_id,
            observed_source_revision="stale", classification="NEW",
        )
        unchanged = (await subject.runtime.get(unclassified.work_id)).item
        assert (stale.status == "stale" and unchanged is not None
                and unchanged.notes == "evidence" and not unchanged.completed)
        foreign = await subject.runtime.works.get(subject.work_id)
        assert foreign is not None
        foreign = await subject.runtime.works.replace(replace(foreign, work_type=category))
        provenance = await subject.service.file_issue(
            category, uuid4(), mode="CLASSIFY", source_work_id=foreign.work_id,
            observed_source_revision=canonical_revision(foreign.work_id, foreign.row_version),
            classification="NEW",
        )
        assert provenance.reason == "source_not_filed_here"
        assert (await subject.runtime.works.get(foreign.work_id)) == foreign


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
    with pytest.raises(ValidationError, match="canonical root must be"):
        update(subject, canonical_root="forbidden")

    assert (stale.status, stale.effect) == ("stale", "not_sent")
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


@pytest.mark.parametrize("case", ["runnable", "review_due", "message_due", "future", "blocker", "unknown", "unpicked", "picked_up", "completed", "inactive"])
async def test_stop_obligations_reads_real_canonical_assignments(subject: Subject, case: str):
    import httpx
    from sqlalchemy import update as sql_update

    from switchstand.agent_mailboxes import (
        CODEX_COORDINATOR_KEY,
        CODEX_COORDINATOR_NAME,
        agent_mailboxes,
    )
    from switchstand.chatgpt_edge import _codex_stop_obligations
    from switchstand.work_policy import validate_resultant_state

    binding = await AgentMailboxState(subject.engine).register_agent(
        "Acceptance", subject.principal.key, "codex:acceptance-session",
    )
    assert binding.status == "ok"
    async with subject.engine.begin() as connection:
        await connection.execute(sql_update(agent_mailboxes).where(
            agent_mailboxes.c.name_key == "acceptance",
        ).values(name_key=CODEX_COORDINATOR_KEY, display_name=CODEX_COORDINATOR_NAME))
    subject.service.messages = MessageState(subject.engine, subject.grants)
    original = await subject.runtime.works.get(subject.work_id)
    assert original is not None
    work = replace(original, owner_key=CODEX_COORDINATOR_NAME)
    if case in {"review_due", "message_due", "future", "blocker", "unpicked"}:
        work = replace(
            work, lifecycle_state="WAITING", next_action_class="NONE", next_action_ref="NONE",
            wait_kind="MESSAGE" if case == "message_due" else "REVIEW",
            unblock_condition="receive exact existing review result",
            next_due=(datetime.now(UTC) + timedelta(days=1 if case == "future" else -1)).isoformat(),
        )
    if case in {"blocker", "unpicked"}:
        work = replace(work, wait_kind="DEPENDENCY", next_due="UNSCHEDULED")
    if case == "unknown":
        work = replace(work, next_action_class="UNKNOWN", next_action_ref="UNKNOWN")
    if case == "picked_up":
        work = replace(work, owner_key="agent:recipient")
    if case == "completed":
        work = replace(work, completed=True, lifecycle_state="TERMINAL",
                       next_action_class="NONE", next_action_ref="NONE")
    validate_resultant_state(**{field: getattr(work, field) for field in (
        "lifecycle_state", "canonical_root", "owner_key", "wait_kind", "unblock_condition",
        "next_due", "next_action_class", "next_action_ref",
    )})
    stored = await subject.runtime.works.replace(work)
    if case == "inactive":
        subject.service.canonical_work_active = False
    result = await _codex_stop_obligations(subject.service, "acceptance-session")
    assert result["status"] == (
        "unknown" if case in {"unknown", "picked_up", "completed", "inactive"} else "ok"
    )
    assert bool(result.get("actions")) == (case in {"runnable", "review_due", "message_due"})
    assert await subject.runtime.works.get(subject.work_id) == stored
    assert (await _codex_stop_obligations(subject.service, "other-session"))["status"] == "unknown"
    # Exercise the real HTTP wrapper: ordinary admission has no OAuth identity on
    # this existing loopback route, unlike direct service calls above.
    app = chatgpt_edge._create_resource_app(
        subject.service, issuer_url="https://issuer.example",
        resource_url="https://resource.example", auth=None, certification_runtime=None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
    ) as client:
        response = await client.post("/internal/codex-stop-mailbox", json={
            "session_id": "acceptance-session", "check_obligations": True,
        })
    assert response.status_code == 200
    assert response.json() == result


@pytest.mark.parametrize("case", ["current", "unlisted", "session", "generation", "owner", "removed", "malformed", "changed_during_read"])
async def test_stop_head_opt_in_is_exact_and_rechecked(
    subject: Subject, tmp_path, monkeypatch: pytest.MonkeyPatch, case: str,
):
    import json
    from pathlib import Path

    binding = await AgentMailboxState(subject.engine).register_agent(
        "Assigned head", subject.principal.key, "codex:head-session",
    )
    assert binding.status == "ok"
    subject.service.messages = MessageState(subject.engine, subject.grants)
    work = await subject.runtime.works.get(subject.work_id)
    assert work is not None
    work = await subject.runtime.works.replace(replace(work, owner_key="agent:assigned-head"))
    config = tmp_path / ".config/switchstand/codex-stop-head.json"
    config.parent.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    value = {
        "session_id": "head-session", "mailbox_name": "Assigned head", "generation": 1,
        "assignments": {str(subject.work_id): "agent:root"},
    }
    if case == "unlisted":
        value["assignments"] = {str(uuid4()): "agent:root"}
    if case == "session":
        value["session_id"] = "other-session"
    if case == "generation":
        value["generation"] = 2
    if case == "owner":
        await subject.runtime.works.replace(replace(work, owner_key="agent:other"))
    config.write_text("{" if case == "malformed" else json.dumps(value))
    if case == "removed":
        config.unlink()
    if case == "changed_during_read":
        original = subject.runtime.works.get

        async def get_and_revoke(work_id):
            result = await original(work_id)
            config.unlink(missing_ok=True)
            return result

        monkeypatch.setattr(subject.runtime.works, "get", get_and_revoke)
    result = await chatgpt_edge._codex_stop_obligations(subject.service, "head-session")
    assert result["status"] == ("ok" if case in {"current", "unlisted"} else "unknown")
    assert bool(result.get("actions")) == (case in {"current", "unlisted"})
    if case in {"current", "unlisted"}:
        assert result["actions"] == [f"{subject.work_id}: implement"]
        assert (await chatgpt_edge._codex_stop_obligations(
            subject.service, "other-session",
        ))["status"] == "unknown"


@pytest.mark.parametrize("name", ["TEST portfolio", "Assigned portfolio"])
async def test_stop_reads_all_agent_assignments_not_the_configured_work_list(
    subject: Subject, tmp_path, monkeypatch: pytest.MonkeyPatch, name: str,
):
    import json
    from pathlib import Path

    from switchstand.workspace_admission import WorkspaceAdmissionState

    registered = await AgentMailboxState(subject.engine).register_agent(
        name, subject.principal.key, "codex:portfolio-session",
    )
    binding = registered.mailbox
    assert registered.status == "ok" and binding is not None
    subject.service.messages = MessageState(subject.engine, subject.grants)
    subject.service.admission_grants = WorkspaceAdmissionState(subject.engine, subject.service.principal)
    original = await subject.runtime.works.get(subject.work_id)
    assert original is not None
    first = replace(original, owner_key=f"agent:{binding.name_key}")
    second = replace(first, work_id=uuid4(), title="Other assignment", next_action_ref="continue other")
    foreign = replace(first, work_id=uuid4(), title="Foreign assignment", owner_key="agent:someone-else")
    await subject.runtime.works.replace(first)
    await subject.runtime.works.create(second)
    await subject.runtime.works.create(foreign)
    foreign_before = await subject.runtime.works.get(foreign.work_id)
    config = tmp_path / ".config/switchstand/codex-stop-head.json"
    config.parent.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    if name.startswith("TEST"):
        config.write_text(json.dumps({"test_prefix": True}))
    else:
        config.write_text(json.dumps({
            "session_id": "portfolio-session", "mailbox_name": name, "generation": binding.generation,
            "assignments": {str(first.work_id): first.owner_key},
        }))
    result = await chatgpt_edge._codex_stop_obligations(subject.service, "portfolio-session")
    assert result["status"] == "ok"
    assert set(result["actions"]) == {
        f"{first.work_id}: implement", f"{second.work_id}: continue other",
    }
    assert await subject.runtime.works.get(foreign.work_id) == foreign_before


@pytest.mark.parametrize("case", ["on", "off", "nonbool", "nonTEST", "revoked"])
async def test_stop_test_prefix_requires_current_explicit_flag(
    subject: Subject, tmp_path, monkeypatch: pytest.MonkeyPatch, case: str,
):
    import json
    from pathlib import Path

    name = "Assigned flag" if case == "nonTEST" else "TEST flag"
    registered = await AgentMailboxState(subject.engine).register_agent(
        name, subject.principal.key, "codex:flag-session",
    )
    binding = registered.mailbox
    assert registered.status == "ok" and binding is not None
    subject.service.messages = MessageState(subject.engine, subject.grants)
    original = await subject.runtime.works.get(subject.work_id)
    assert original is not None
    await subject.runtime.works.replace(replace(original, owner_key=f"agent:{binding.name_key}"))
    config = tmp_path / ".config/switchstand/codex-stop-head.json"
    config.parent.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    flag = False if case == "off" else "true" if case == "nonbool" else True
    config.write_text(json.dumps({"test_prefix": flag}))
    if case == "revoked":
        original_get = subject.runtime.works.get

        async def get_and_revoke(work_id):
            result = await original_get(work_id)
            config.write_text(json.dumps({"test_prefix": False}))
            return result

        monkeypatch.setattr(subject.runtime.works, "get", get_and_revoke)
    result = await chatgpt_edge._codex_stop_obligations(subject.service, "flag-session")
    assert result["status"] == ("ok" if case == "on" else "unknown")
    assert result.get("actions", []) == ([f"{subject.work_id}: implement"] if case == "on" else [])
    if case == "revoked":
        assert result["reason"] == "head_opt_in_changed"
