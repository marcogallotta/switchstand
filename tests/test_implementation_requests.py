import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_revision,
    canonical_work,
)
from switchstand.contracts import LaunchAuthority
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext, WorkGrant
from switchstand.human_reviews import HumanReviewConsequence, HumanReviewState
from switchstand.implementation_requests import ImplementationRequestState
from switchstand.state import PostgresState
from switchstand.task_runs import task_run_requests

PACKAGE = UUID("45000000-0000-4000-8000-000000000001")
PRINCIPAL = PrincipalContext(
    issuer="fixture", subject="implementation-owner", client_id="codex", assurance="test"
)


@pytest.fixture
async def subject(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for implementation-request tests")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "DROP TABLE IF EXISTS alembic_version, human_review_consequences, "
                "activation_obligation_revisions, task_run_results, task_run_executions, "
                "task_run_requests, "
                "failure_resolutions, failure_records, work_migration_receipts, "
                "outcome_state_revisions, human_trajectory_revisions, "
                "agent_mailbox_transfer_requests, agent_mailboxes, work_event_handles, "
                "lifecycle_obligations, message_projection, message_deliveries, messages, "
                "effect_intents, work_grants, work_events, project_memberships, projects, "
                "work_parents, work_dependencies, legacy_work_aliases, canonical_work, "
                "work_handles CASCADE"
            )
        )
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    state = PostgresState(engine)
    await state.bind_reserved(PACKAGE, "local", str(PACKAGE))
    works = CanonicalWorkRepository(engine)
    await works.create(CurrentWork(
        PACKAGE, "Approved package", False, "exact package", lifecycle_state="CURRENT"
    ))
    grant = WorkGrant(
        id=uuid4(),
        version=1,
        principal=PRINCIPAL,
        authority=LaunchAuthority(active_work_id=PACKAGE),
        operations=frozenset({"implementation_request"}),
        issuer="fixture",
        provenance="direct assignment fixture",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await GrantState(engine).issue(grant, None)
    yield ImplementationRequestState(engine, works), HumanReviewState(engine, works), engine, grant
    await engine.dispose()


def consequence() -> HumanReviewConsequence:
    return HumanReviewConsequence(
        package_work_id=PACKAGE,
        package_revision=canonical_revision(PACKAGE, 1),
        implementation_scope=("Implement the exact reviewed package",),
        implementation_target="the package WorkId in an owned writer",
        excluded_effects=("deployment", "activation", "provider production writes"),
    )


async def approve(reviews: HumanReviewState) -> HumanReviewConsequence:
    value = consequence()
    proposed = await reviews.propose(value)
    assert proposed.record is not None
    approved = await reviews.submit(
        proposed.record.consequence_id, PACKAGE, value.package_revision, "APPROVED"
    )
    assert approved.status == "RECORDED"
    return value


async def request_count(engine) -> int:
    async with engine.connect() as connection:
        return await connection.scalar(select(func.count()).select_from(task_run_requests)) or 0


async def test_approved_current_send_authority_creates_one_inert_request(subject):
    facade, reviews, engine, grant = subject
    approved = await approve(reviews)
    operation_id = uuid4()

    applied, recovered = await asyncio.gather(
        facade.request(PRINCIPAL, operation_id, PACKAGE, approved.package_revision),
        facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision),
    )

    assert {applied.status, recovered.status} == {"APPLIED", "REPLAYED"}
    assert applied.task_run_request_id == recovered.task_run_request_id
    assert applied.delivery_state == "PENDING"
    assert applied.next_action == "await managed worker pickup"
    assert await request_count(engine) == 1
    async with engine.connect() as connection:
        row = (await connection.execute(select(task_run_requests))).mappings().one()
    assert row["task_kind"] == "IMPLEMENTATION"
    assert row["authorization_ref"] == (
        f"human-review/{approved.consequence_id}/{approved.digest}"
    )
    assert row["send_authority_ref"] == f"grant/{grant.id}/1/{PRINCIPAL.key}"
    assert row["execution_work_id"] == PACKAGE
    assert row["result_contract"]["return_to_work_id"] == str(PACKAGE)
    assert row["result_contract"]["implementation_scope"] == list(
        approved.implementation_scope
    )
    assert row["result_contract"]["excluded_effects"] == list(approved.excluded_effects)
    assert row["result_contract"]["milestones"] == ["IMPLEMENTED", "MERGED", "ACTIVATED"]

    async with engine.begin() as connection:
        await connection.execute(
            update(task_run_requests).values(objective="Corrupt stored implementation intent.")
        )
    corrupt = await facade.request(PRINCIPAL, operation_id, PACKAGE, approved.package_revision)
    assert (corrupt.status, corrupt.reason) == ("UNKNOWN", "state_unavailable")


async def test_no_approval_or_other_principal_creates_zero_requests(subject):
    facade, reviews, engine, _ = subject
    denied = await facade.request(
        PRINCIPAL, uuid4(), PACKAGE, canonical_revision(PACKAGE, 1)
    )
    assert (denied.status, denied.reason) == ("DENIED", "human_review_not_approved")

    await approve(reviews)
    other = PRINCIPAL.model_copy(update={"subject": "other-agent"})
    unauthorized = await facade.request(
        other, uuid4(), PACKAGE, canonical_revision(PACKAGE, 1)
    )
    assert (unauthorized.status, unauthorized.reason) == ("DENIED", "no_send_authority")
    assert await request_count(engine) == 0


async def test_exact_replay_survives_revoke_but_changed_replay_conflicts(subject):
    facade, reviews, engine, grant = subject
    approved = await approve(reviews)
    operation_id = uuid4()
    first = await facade.request(PRINCIPAL, operation_id, PACKAGE, approved.package_revision)
    revoked = grant.model_copy(
        update={"id": uuid4(), "version": 2, "state": "revoked"}
    )
    await GrantState(engine).issue(revoked, 1)

    replay = await facade.request(PRINCIPAL, operation_id, PACKAGE, approved.package_revision)
    changed = await facade.request(PRINCIPAL, operation_id, uuid4(), approved.package_revision)

    assert replay.status == "REPLAYED"
    assert replay.task_run_request_id == first.task_run_request_id
    assert (changed.status, changed.reason) == ("CONFLICT", "operation_identity_conflict")
    assert await request_count(engine) == 1


async def test_stale_package_or_revoked_authority_creates_zero_requests(subject):
    facade, reviews, engine, grant = subject
    approved = await approve(reviews)
    stale = await facade.request(PRINCIPAL, uuid4(), PACKAGE, "pg_stale")
    assert (stale.status, stale.reason) == ("STALE", "package_revision_changed")

    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).values(completed=True))
    terminal = await facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision)
    assert (terminal.status, terminal.reason) == ("DENIED", "package_not_active")
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).values(
            completed=False, lifecycle_state="TERMINAL"
        ))
    terminal = await facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision)
    assert (terminal.status, terminal.reason) == ("DENIED", "package_not_active")
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).values(lifecycle_state="CURRENT"))

    revoked = grant.model_copy(
        update={"id": uuid4(), "version": 2, "state": "revoked"}
    )
    await GrantState(engine).issue(revoked, 1)
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision)
    assert (denied.status, denied.reason) == ("STALE", "send_authority_not_current")
    assert await request_count(engine) == 0


async def test_hold_review_creates_zero_requests(subject):
    facade, reviews, engine, _ = subject
    value = consequence()
    proposed = await reviews.propose(value)
    assert proposed.record is not None
    held = await reviews.submit(proposed.record.consequence_id, PACKAGE, value.package_revision, "HOLD")
    assert held.status == "RECORDED"
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, value.package_revision)
    assert (denied.status, denied.reason) == ("DENIED", "human_review_not_approved")
    assert await request_count(engine) == 0
