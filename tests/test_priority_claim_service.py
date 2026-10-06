import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.canonical_relations import projects
from switchstand.canonical_work import (
    CanonicalWorkRepository,
    canonical_metadata,
    canonical_revision,
    canonical_work,
)
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext, WorkGrant
from switchstand.priority_claim_service import (
    HumanPriorityClear,
    HumanPrioritySet,
    PriorityClaimService,
    PriorityClaimWrite,
)
from switchstand.priority_claims import (
    NewPriorityClaim,
    PriorityClaimRepository,
    priority_claims,
)
from switchstand.state import metadata, work_handles
from switchstand.workspace_admission import WorkspaceAdmissionState


@pytest.fixture
async def subject(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    work, target, project = uuid4(), uuid4(), uuid4()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.run_sync(metadata.create_all)
        await connection.run_sync(lambda sync: canonical_metadata.create_all(
            sync, tables=[canonical_work, projects, priority_claims], checkfirst=True,
        ))
        await connection.execute(canonical_work.insert(), [{
            "work_id": value, "title": str(value), "normalized_title": str(value),
            "completed": False, "notes": "", "row_version": 1,
        } for value in (work, target)])
        await connection.execute(projects.insert().values(project_id=project, name="Project"))
        await connection.execute(work_handles.insert(), [{
            "id": value, "provider": "canonical", "provider_work_id": str(value),
        } for value in (work, target)])
    principal = PrincipalContext(
        issuer="test", subject=str(uuid4()), client_id="tests", assurance="test",
    )
    grants = GrantState(engine)
    grant = WorkGrant(
        id=uuid4(), version=1, principal=principal,
        authority={"active_work_id": work}, operations=frozenset({
            "work_get", "priority_claim",
        }), issuer="tests", provenance="exact launch test", scope="launch",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        priority_claim_qualification="test:launch-owner",
    )
    await grants.issue(grant, None)
    repository = PriorityClaimRepository(engine)
    yield (
        PriorityClaimService(repository, CanonicalWorkRepository(engine)),
        repository, grants, principal, grant, work, target, project,
    )
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.run_sync(lambda sync: canonical_metadata.drop_all(
            sync, tables=[priority_claims, projects, canonical_work], checkfirst=True,
        ))
    await engine.dispose()


def request(work, operation=None, **changes):
    values = {
        "api_version": "1", "operation_id": operation or uuid4(), "work_id": work,
        "grant_version": 1, "observed_revision": canonical_revision(work, 1),
        "relation_kind": "BAND", "band": "HIGH", "rationale": "inspect first",
    }
    values.update(changes)
    return PriorityClaimWrite(**values)


async def test_agent_create_replay_supersede_stale_and_scope(subject):
    service, repository, grants, principal, grant, work, target, _project = subject
    first_request = request(work)
    unknown = service.guard(first_request, "unknown", "prepared", possible=True)
    await grants.prepare({"request": first_request.model_dump(mode="json"),
                          "qualification": "test:launch-owner"}, grant,
                         service._fingerprint(principal, first_request), unknown)
    await repository.record(service._new(first_request, grant.id))
    blocked = await service.record(grants, principal, request(work))
    assert blocked.reason == "target_has_unresolved_effect"
    assert blocked.blocked_by and blocked.blocked_by.operation_id == first_request.operation_id
    first = await service.record(grants, principal, first_request)
    replay = await service.record(grants, principal, first_request)
    assert first.status == replay.status == "ok" and first.receipt == replay.receipt
    assert first.receipt is not None
    claim_id = first.receipt.claim_id
    conflict = await service.record(grants, principal, first_request.model_copy(update={
        "rationale": "changed reuse",
    }))
    assert conflict.reason == "operation_identity_conflict"

    second = await service.record(grants, principal, request(
        work, relation_kind="BEFORE", band=None, relation_target_id=target,
        supersedes_claim_id=claim_id,
    ))
    assert second.status == "ok"
    current = await service.current("WORK", work)
    assert len(current.claims) == 1 and current.claims[0].relation_kind == "BEFORE"
    assert current.claims[0].currentness == "CURRENT"
    assert len(await repository.provenance(second.receipt.claim_id)) == 2  # type: ignore[union-attr]

    stale = await service.record(grants, principal, request(
        work, observed_revision=canonical_revision(work, 2),
    ))
    outside = await service.record(grants, principal, request(target))
    assert stale.reason == "source_revision_changed"
    assert outside.reason == "claim_scope_not_granted"

    workspace = grant.model_copy(update={"version": 2, "scope": "workspace"})
    await grants.issue(workspace, 1)
    denied = await service.record(grants, principal, request(work).model_copy(update={
        "grant_version": 2,
    }))
    assert denied.reason == "claim_scope_not_granted"


async def test_journal_read_failure_is_fail_closed_unknown(subject):
    service, _repository, grants, principal, _grant, work, *_ = subject
    async with grants.engine.begin() as connection:
        await connection.execute(text("ALTER TABLE effect_intents RENAME TO hidden_intents"))
    try:
        outcome = await service.record(grants, principal, request(work))
    finally:
        async with grants.engine.begin() as connection:
            await connection.execute(text("ALTER TABLE hidden_intents RENAME TO effect_intents"))
    assert (outcome.status, outcome.effect, outcome.retry) == ("unknown", "unknown", "reconcile")


async def test_prepared_without_claim_is_terminal_and_unfences_target(subject):
    service, _repository, grants, principal, grant, work, *_ = subject
    prepared = request(work)
    await grants.prepare({"request": prepared.model_dump(mode="json"),
                          "qualification": "test:launch-owner"}, grant,
                         service._fingerprint(principal, prepared),
                         service.guard(prepared, "unknown", "prepared", possible=True))
    recovered = await service.record(grants, principal, prepared)
    assert (recovered.status, recovered.effect, recovered.reason) == (
        "not_applied", "not_sent", "claim_absence_confirmed",
    )
    assert await service.record(grants, principal, prepared) == recovered
    assert (await service.record(grants, principal, request(work))).effect == "applied"


async def test_project_read_does_not_propagate_and_human_write_is_not_a_contract(subject):
    service, repository, _grants, _principal, _grant, work, _target, project = subject
    await repository.record(NewPriorityClaim(
        claim_id=uuid4(), claim_kind="HUMAN_PRIORITY", subject_kind="PROJECT",
        subject_id=project, relation_kind="HOLD", rationale="human hold",
        source_label="HUMAN", source_ref="trusted-future-seam",
    ))
    project_result = await service.current("PROJECT", project)
    work_result = await service.current("WORK", work)
    assert [claim.claim_kind for claim in project_result.claims] == ["HUMAN_PRIORITY"]
    assert work_result.claims == ()
    with pytest.raises(ValidationError):
        request(work, relation_kind="NONE", band=None)


async def test_workspace_human_set_clear_replay_and_project_scope(subject, monkeypatch):
    service, repository, _grants, principal, _grant, work, target, project = subject

    async def current_principal():
        return principal

    admission = WorkspaceAdmissionState(repository.engine, current_principal)
    set_operation = uuid4()
    set_request = HumanPrioritySet(
        api_version="1", operation_id=set_operation, subject_kind="WORK",
        subject_id=work, observed_revision=canonical_revision(work, 1),
        relation_kind="BAND", band="HIGH", rationale="Marco says high",
    )
    first = await service.human_set(admission, principal, set_request)
    replay = await service.human_set(admission, principal, set_request)
    assert first.status == replay.status == "ok"
    assert first.receipt == replay.receipt
    assert first.receipt is not None and first.receipt.action == "SET"
    assert first.receipt.subject_kind == "WORK"
    human = (await service.current("WORK", work)).claims[0]
    assert human.claim_kind == "HUMAN_PRIORITY"
    assert human.source_observed_revision is None and human.currentness == "CURRENT"

    async with repository.engine.begin() as connection:
        await connection.execute(canonical_work.update().where(
            canonical_work.c.work_id == work
        ).values(row_version=2))
    assert (await service.human_set(admission, principal, set_request)).status == "ok"
    changed_set = set_request.model_copy(update={
        "observed_revision": canonical_revision(work, 2),
    })
    changed = await service.human_set(admission, principal, changed_set)
    assert (changed.status, changed.effect, changed.reason) == (
        "denied", "not_sent", "operation_identity_conflict",
    )
    stale_new = set_request.model_copy(update={
        "operation_id": uuid4(), "observed_revision": canonical_revision(work, 1),
    })
    stale = await service.human_set(admission, principal, stale_new)
    assert (stale.status, stale.effect, stale.reason) == (
        "stale", "not_sent", "source_revision_changed",
    )
    assert [claim.claim_id for claim in (await service.current("WORK", work)).claims] == [
        human.claim_id
    ]

    clear_operation = uuid4()
    clear_request = HumanPriorityClear(
        api_version="1", operation_id=clear_operation, subject_kind="WORK",
        subject_id=work, claim_id=human.claim_id,
        observed_revision=canonical_revision(work, 2),
    )
    cleared = await service.human_clear(admission, principal, clear_request)
    assert cleared.status == "ok" and cleared.receipt is not None
    assert cleared.receipt.action == "CLEAR"
    assert cleared.receipt.cleared_claim_id == human.claim_id
    assert (await service.current("WORK", work)).claims == ()
    tombstone = await repository.get(cleared.receipt.claim_id)
    assert tombstone is not None and tombstone.state == "SUPERSEDED"
    assert tombstone.supersedes_claim_id == human.claim_id

    real_get = repository.get
    miss_tombstone = True

    async def race_get(claim_id):
        nonlocal miss_tombstone
        if claim_id == tombstone.claim_id and miss_tombstone:
            miss_tombstone = False
            return None
        return await real_get(claim_id)

    monkeypatch.setattr(repository, "get", race_get)
    assert (await service.human_clear(admission, principal, clear_request)).status == "ok"
    miss_tombstone = True
    raced_changed_subject = clear_request.model_copy(update={
        "subject_id": target, "observed_revision": canonical_revision(target, 1),
    })
    raced = await service.human_clear(admission, principal, raced_changed_subject)
    assert (raced.status, raced.effect, raced.reason) == (
        "denied", "not_sent", "operation_identity_conflict",
    )
    monkeypatch.setattr(repository, "get", real_get)

    async with repository.engine.begin() as connection:
        await connection.execute(canonical_work.update().where(
            canonical_work.c.work_id == work
        ).values(row_version=3))
    assert (await service.human_clear(admission, principal, clear_request)).status == "ok"
    changed_clear = clear_request.model_copy(update={
        "observed_revision": canonical_revision(work, 3),
    })
    changed = await service.human_clear(admission, principal, changed_clear)
    assert (changed.status, changed.reason) == ("denied", "operation_identity_conflict")
    changed_subject = clear_request.model_copy(update={
        "subject_id": target, "observed_revision": canonical_revision(target, 1),
    })
    changed = await service.human_clear(admission, principal, changed_subject)
    assert (changed.status, changed.reason) == ("denied", "operation_identity_conflict")

    project_set = HumanPrioritySet(
        api_version="1", operation_id=uuid4(), subject_kind="PROJECT",
        subject_id=project, relation_kind="HOLD", rationale="hold the project",
    )
    project_result = await service.human_set(admission, principal, project_set)
    assert project_result.status == "ok" and project_result.receipt is not None
    assert project_result.receipt.work_id is None
    assert [item.claim_kind for item in (await service.current(
        "PROJECT", project
    )).claims] == ["HUMAN_PRIORITY"]


def test_human_priority_requires_work_revision_only_for_work():
    common = {
        "api_version": "1", "operation_id": uuid4(), "subject_id": uuid4(),
        "relation_kind": "HOLD", "rationale": "explicit",
    }
    with pytest.raises(ValidationError):
        HumanPrioritySet(subject_kind="WORK", **common)
    with pytest.raises(ValidationError):
        HumanPrioritySet(subject_kind="PROJECT", observed_revision="forbidden", **common)
