import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.canonical_work import (
    CanonicalWorkRepository,
    canonical_metadata,
    canonical_revision,
    canonical_work,
)
from switchstand.human_reviews import (
    HumanReviewConsequence,
    HumanReviewState,
    human_review_consequences,
)

PACKAGE = UUID("60000000-0000-4000-8000-000000000001")


class ConsequenceSource:
    def __init__(self, revision: str):
        self.value = HumanReviewConsequence(
            package_work_id=PACKAGE,
            package_revision=revision,
            implementation_scope=("Implement the reviewed package",),
            implementation_target="repository main through normal reviewed landing",
            excluded_effects=("deployment", "activation", "provider production writes"),
        )

@pytest.fixture
async def subject(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for Human Review tests")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text(
            "DROP TABLE IF EXISTS human_review_consequences, canonical_work CASCADE"
        ))
        await connection.run_sync(
            lambda sync: canonical_metadata.create_all(
                sync, tables=[canonical_work, human_review_consequences]
            )
        )
        await connection.execute(insert(canonical_work).values(
            work_id=PACKAGE,
            title="Reviewed package",
            normalized_title="reviewed package",
            completed=False,
            notes="exact package",
            row_version=1,
        ))
    revision = canonical_revision(PACKAGE, 1)
    source = ConsequenceSource(revision)
    yield HumanReviewState(engine, CanonicalWorkRepository(engine)), source, engine
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS human_review_consequences"))
    await engine.dispose()


async def test_approval_binds_exact_consequence_and_only_marks_ready(subject):
    store, source, engine = subject
    proposed = await store.propose(source.value)
    assert proposed.status == "PREPARED"
    prepared = await store.prepare(PACKAGE, canonical_revision(PACKAGE, 1))
    assert prepared.status == "REPLAYED"
    assert prepared.record is not None
    assert prepared.record.state == "PENDING"

    approved = await store.submit(
        prepared.record.consequence_id, PACKAGE, prepared.record.package_revision, "APPROVED"
    )
    assert approved.status == "RECORDED"
    assert approved.record is not None
    assert approved.record.state == "READY_FOR_IMPLEMENTATION"
    assert approved.record.decision == "APPROVED"
    assert set(canonical_metadata.tables).isdisjoint({"task_run_requests"})

    async with engine.connect() as connection:
        rows = (await connection.execute(human_review_consequences.select())).mappings().all()
    assert len(rows) == 1
    assert rows[0]["state"] == "READY_FOR_IMPLEMENTATION"

    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(update(human_review_consequences).values(
                decision="HOLD", state="READY_FOR_IMPLEMENTATION"
            ))


@pytest.mark.parametrize("decision", ["WAIT", "HOLD", "NO_DISPATCH"])
async def test_non_approval_decisions_create_no_ready_state(subject, decision):
    store, source, _ = subject
    await store.propose(source.value)
    prepared = await store.prepare(PACKAGE, canonical_revision(PACKAGE, 1))
    assert prepared.record is not None
    result = await store.submit(
        prepared.record.consequence_id, PACKAGE, prepared.record.package_revision, decision
    )
    assert result.status == "RECORDED"
    assert result.record is not None
    assert result.record.state == decision


async def test_changed_package_or_consequence_is_stale_with_zero_decision(subject):
    store, source, engine = subject
    await store.propose(source.value)
    prepared = await store.prepare(PACKAGE, canonical_revision(PACKAGE, 1))
    assert prepared.record is not None

    changed_proposal = source.value.model_copy(update={
        "excluded_effects": (*source.value.excluded_effects, "credential changes")
    })
    conflict = await store.propose(changed_proposal)
    assert conflict.status == "CONFLICT" and conflict.reason == "consequence_changed"
    async with engine.connect() as connection:
        row = (await connection.execute(human_review_consequences.select())).mappings().one()
    assert row["decision"] is None and row["state"] == "PENDING"

    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).where(
            canonical_work.c.work_id == PACKAGE
        ).values(row_version=2))
    stale = await store.submit(
        prepared.record.consequence_id, PACKAGE, prepared.record.package_revision, "APPROVED"
    )
    assert stale.status == "STALE" and stale.reason == "package_revision_changed"
    source.value = changed_proposal.model_copy(update={
        "package_revision": canonical_revision(PACKAGE, 2)
    })
    reproposed = await store.propose(source.value)
    assert reproposed.status == "PREPARED"
    current = await store.prepare(PACKAGE, source.value.package_revision)
    assert current.status == "REPLAYED" and current.record == reproposed.record


async def test_prepare_and_submit_replay_are_exact_and_concurrent(subject):
    store, source, engine = subject
    revision = canonical_revision(PACKAGE, 1)
    left, right = await asyncio.gather(
        store.propose(source.value), store.propose(source.value)
    )
    assert {left.status, right.status} == {"PREPARED", "REPLAYED"}
    record = left.record or right.record
    assert record is not None

    first, replay = await asyncio.gather(
        store.submit(record.consequence_id, PACKAGE, revision, "APPROVED"),
        store.submit(record.consequence_id, PACKAGE, revision, "APPROVED"),
    )
    assert {first.status, replay.status} == {"RECORDED", "REPLAYED"}
    conflict = await store.submit(record.consequence_id, PACKAGE, revision, "HOLD")
    assert conflict.status == "CONFLICT" and conflict.reason == "decision_conflict"
    async with engine.connect() as connection:
        rows = (await connection.execute(human_review_consequences.select())).all()
    assert len(rows) == 1


async def test_missing_package_and_mismatched_reader_fail_closed(subject):
    store, source, _ = subject
    missing = await store.prepare(uuid4(), "pg_missing")
    assert missing.status == "DENIED" and missing.reason == "package_not_found"
    source.value = source.value.model_copy(update={"package_work_id": uuid4()})
    mismatch = await store.propose(source.value)
    assert mismatch.status == "DENIED" and mismatch.reason == "package_not_found"


async def test_concurrent_different_proposals_admit_only_one_exact_intent(subject):
    store, source, engine = subject
    changed = source.value.model_copy(update={
        "implementation_target": "a different implementation target"
    })

    first, second = await asyncio.gather(store.propose(source.value), store.propose(changed))

    assert {first.status, second.status} == {"PREPARED", "CONFLICT"}
    async with engine.connect() as connection:
        rows = (await connection.execute(human_review_consequences.select())).mappings().all()
    assert len(rows) == 1
    admitted = first.record or second.record
    assert admitted is not None
    assert rows[0]["consequence_digest"] == admitted.consequence_digest


async def test_duplicate_proposal_rows_fail_closed(subject):
    store, source, engine = subject
    prepared = await store.propose(source.value)
    assert prepared.record is not None
    duplicate = source.value.model_copy(update={"implementation_target": "duplicate target"})
    async with engine.begin() as connection:
        await connection.execute(insert(human_review_consequences).values(
            consequence_id=duplicate.consequence_id,
            package_work_id=PACKAGE,
            package_revision=source.value.package_revision,
            consequence_digest=duplicate.digest,
            consequence=duplicate.model_dump(mode="json"),
            state="PENDING",
        ))

    read = await store.prepare(PACKAGE, source.value.package_revision)
    submit = await store.submit(
        prepared.record.consequence_id, PACKAGE, source.value.package_revision, "APPROVED"
    )
    assert read.status == submit.status == "UNKNOWN"
    assert read.reason == submit.reason == "state_unavailable"
