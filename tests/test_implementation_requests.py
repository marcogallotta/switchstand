import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
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
from switchstand.reviews import BaselineReviewProof
from switchstand.state import PostgresState
from switchstand.task_runs import task_run_requests

PACKAGE = UUID("45000000-0000-4000-8000-000000000001")
PRINCIPAL = PrincipalContext(issuer="fixture", subject="implementation-owner", client_id="codex", assurance="test")

@pytest.fixture
async def subject(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL") or pytest.skip("TEST_DATABASE_URL is required for implementation-request tests")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    state, works = PostgresState(engine), CanonicalWorkRepository(engine)
    await state.bind_reserved(PACKAGE, "local", str(PACKAGE))
    await works.create(CurrentWork(PACKAGE, "Approved package", False, "exact package", lifecycle_state="CURRENT"))
    grant = WorkGrant(id=uuid4(), version=1, principal=PRINCIPAL, authority=LaunchAuthority(active_work_id=PACKAGE), operations=frozenset({"implementation_request"}), issuer="fixture", provenance="direct assignment fixture", expires_at=datetime.now(UTC) + timedelta(hours=1))
    await GrantState(engine).issue(grant, None)
    occurrences = AsyncMock()
    occurrences.baseline_pass_status_in_transaction.return_value = BaselineReviewProof(
        status="NOT_PASS", reason="no matching baseline review"
    )
    occurrences.pass_status_in_transaction.return_value = "PASS"
    yield ImplementationRequestState(engine, works, occurrences), HumanReviewState(engine, works), engine, grant, occurrences
    await engine.dispose()

def consequence() -> HumanReviewConsequence:
    return HumanReviewConsequence(package_work_id=PACKAGE, package_revision=canonical_revision(PACKAGE, 1), implementation_scope=("Implement the exact reviewed package",), implementation_target="the package WorkId in an owned writer", excluded_effects=("deployment", "activation", "provider production writes"))


def v2_consequence() -> HumanReviewConsequence:
    return HumanReviewConsequence.model_validate({**consequence().model_dump(),
        "approval_effect": "CLEAR_HUMAN_REVIEW_FOR_NAMED_CONTINUATION",
        "baseline": {
            "human_source_refs": ("Marco 2026-10-16",),
            "opening": {"what_you_asked": "Approved redesign", "what_this_does": "Binds admission", "what_this_does_not_or_changes_for_you": "No activation", "size": "600-1000 lines"},
            "approved_meaning": ("Preserve the approved baseline",),
            "protected_requirements": ("Only preserving review dispatches",),
            "approved_non_goals": ("No workflow engine",),
            "decision_context": {"size_or_burden": ("600-1000 lines",), "scale_assumptions": (), "material_risks_and_boundaries": ("Separate activation",)},
            "continuation_effects": ("IMPLEMENT", "LAND"),
        },
    })


async def approve(reviews: HumanReviewState) -> HumanReviewConsequence:
    value, proposed = consequence(), await reviews.propose(consequence())
    assert proposed.record is not None
    approved = await reviews.submit(proposed.record.consequence_id, PACKAGE, value.package_revision, "APPROVED")
    assert approved.status == "RECORDED"
    return value


async def request_count(engine) -> int:
    async with engine.connect() as connection:
        return await connection.scalar(select(func.count()).select_from(task_run_requests)) or 0


async def test_approved_current_send_authority_creates_one_inert_request(subject):
    facade, reviews, engine, grant, occurrences = subject
    approved, operation_id = await approve(reviews), uuid4()
    applied, recovered = await asyncio.gather(facade.request(PRINCIPAL, operation_id, PACKAGE, approved.package_revision), facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision))
    assert (applied.status, recovered.status, applied.task_run_request_id) == ("APPLIED", "REPLAYED", recovered.task_run_request_id)
    assert occurrences.pass_status_in_transaction.call_args.args[1:] == (
        PACKAGE, approved.package_revision, "IMPLEMENTATION", None,
    )
    assert (applied.delivery_state, applied.next_action, await request_count(engine)) == ("PENDING", "await managed worker pickup", 1)
    async with engine.connect() as connection:
        row = (await connection.execute(select(task_run_requests))).mappings().one()
    assert (row["task_kind"], row["authorization_ref"], row["send_authority_ref"], row["execution_work_id"]) == ("IMPLEMENTATION", f"human-review/{approved.consequence_id}/{approved.digest}", f"grant/{grant.id}/1/{PRINCIPAL.key}", PACKAGE)
    contract = row["result_contract"]
    assert (contract["return_to_work_id"], contract["implementation_scope"], contract["excluded_effects"], contract["milestones"]) == (str(PACKAGE), list(approved.implementation_scope), list(approved.excluded_effects), ["IMPLEMENTED"])
    async with engine.begin() as connection:
        await connection.execute(update(task_run_requests).values(objective="corrupt"))
    corrupt = await facade.request(PRINCIPAL, operation_id, PACKAGE, approved.package_revision)
    assert (corrupt.status, corrupt.reason) == ("UNKNOWN", "state_unavailable")


async def test_invalid_currentness_and_authority_create_zero_requests(subject):
    facade, reviews, engine, grant, occurrences = subject
    revision = canonical_revision(PACKAGE, 1)
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, revision)
    assert (denied.status, denied.reason) == ("DENIED", "human_review_not_approved")
    approved = await approve(reviews)
    denied = await facade.request(PRINCIPAL.model_copy(update={"subject": "other-agent"}), uuid4(), PACKAGE, revision)
    assert (denied.status, denied.reason) == ("DENIED", "no_send_authority")
    occurrences.baseline_pass_status_in_transaction.return_value = BaselineReviewProof(status="UNKNOWN", reason="unreadable")
    unavailable = await facade.request(PRINCIPAL, uuid4(), PACKAGE, revision)
    assert (unavailable.status, unavailable.reason, await request_count(engine)) == ("UNKNOWN", "baseline_review_unavailable", 0)
    occurrences.baseline_pass_status_in_transaction.return_value = BaselineReviewProof(status="NOT_PASS", reason="no matching baseline review")
    for review_status, expected in (("NOT_PASS", ("DENIED", "independent_review_not_passed")), ("UNKNOWN", ("UNKNOWN", "independent_review_unavailable"))):
        occurrences.pass_status_in_transaction.return_value = review_status
        denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, revision)
        assert (denied.status, denied.reason) == expected
    occurrences.pass_status_in_transaction.return_value = "PASS"
    stale = await facade.request(PRINCIPAL, uuid4(), PACKAGE, "pg_stale")
    assert (stale.status, stale.reason) == ("STALE", "package_revision_changed")
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).values(completed=True))
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision)
    assert (denied.status, denied.reason) == ("DENIED", "package_not_active")
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).values(completed=False, lifecycle_state="TERMINAL"))
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision)
    assert (denied.status, denied.reason) == ("DENIED", "package_not_active")
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).values(lifecycle_state="CURRENT"))
    await GrantState(engine).issue(grant.model_copy(update={"id": uuid4(), "version": 2, "state": "revoked"}), 1)
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision)
    assert (denied.status, denied.reason, await request_count(engine)) == ("STALE", "send_authority_not_current", 0)


async def test_exact_replay_survives_revoke_but_changed_replay_conflicts(subject):
    facade, reviews, engine, grant, _ = subject
    approved, operation_id = await approve(reviews), uuid4()
    first = await facade.request(PRINCIPAL, operation_id, PACKAGE, approved.package_revision)
    await GrantState(engine).issue(grant.model_copy(update={"id": uuid4(), "version": 2, "state": "revoked"}), 1)
    replay = await facade.request(PRINCIPAL, operation_id, PACKAGE, approved.package_revision)
    changed = await facade.request(PRINCIPAL, operation_id, uuid4(), approved.package_revision)
    assert (replay.status, replay.task_run_request_id, changed.status, changed.reason, await request_count(engine)) == ("REPLAYED", first.task_run_request_id, "CONFLICT", "operation_identity_conflict", 1)


async def test_hold_review_creates_zero_requests(subject):
    facade, reviews, engine, _, _ = subject
    value, proposed = consequence(), await reviews.propose(consequence())
    assert proposed.record is not None
    held = await reviews.submit(proposed.record.consequence_id, PACKAGE, value.package_revision, "HOLD")
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, value.package_revision)
    assert (held.status, denied.status, denied.reason, await request_count(engine)) == ("RECORDED", "DENIED", "human_review_not_approved", 0)


async def test_v2_baseline_and_exact_preimplementation_pass_bind_full_authorization(subject):
    facade, reviews, engine, grant, occurrences = subject
    approved = v2_consequence()
    proposed = await reviews.propose(approved)
    assert proposed.record is not None
    await reviews.submit(approved.consequence_id, PACKAGE, approved.package_revision, "APPROVED")
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).where(
            canonical_work.c.work_id == PACKAGE
        ).values(row_version=2))
    revision = canonical_revision(PACKAGE, 2)
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, revision)
    assert (denied.status, denied.reason, await request_count(engine)) == ("DENIED", "baseline_review_not_passed", 0)
    review_id, verdict_digest = uuid4(), "a" * 64
    occurrences.baseline_pass_status_in_transaction.return_value = BaselineReviewProof(
        status="PASS", baseline_id=approved.consequence_id, phase="PRE_IMPLEMENTATION",
        review_id=review_id, verdict_digest=verdict_digest, reason="exact preserving review",
    )
    applied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, revision)
    assert (applied.status, await request_count(engine)) == ("APPLIED", 1)
    occurrences.pass_status_in_transaction.assert_not_called()
    async with engine.connect() as connection:
        row = (await connection.execute(select(task_run_requests))).mappings().one()
    expected_ref = f"human-review-v2/{approved.consequence_id}/{approved.digest}/review/{review_id}/{verdict_digest}"
    assert row["authorization_ref"] == expected_ref
    assert row["send_authority_ref"] == f"grant/{grant.id}/1/{PRINCIPAL.key}"
    contract = row["result_contract"]
    assert (contract["baseline_id"], contract["baseline_digest"], contract["preimplementation_review_id"], contract["preimplementation_verdict_digest"]) == (str(approved.consequence_id), approved.digest, str(review_id), verdict_digest)
    assert (contract["continuation_effects"], contract["milestones"], contract["excluded_effects"]) == (["IMPLEMENT", "LAND"], ["IMPLEMENTED", "MERGED"], list(approved.excluded_effects))
