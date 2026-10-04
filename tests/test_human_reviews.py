import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

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

    async def read(
        self, connection: AsyncConnection, package_work_id: UUID
    ) -> HumanReviewConsequence | None:
        del connection
        return self.value if package_work_id == PACKAGE else None


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
    yield HumanReviewState(engine, CanonicalWorkRepository(engine), source.read), source, engine
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS human_review_consequences"))
    await engine.dispose()


async def test_approval_binds_exact_consequence_and_only_marks_ready(subject):
    store, _, engine = subject
    prepared = await store.prepare(PACKAGE, canonical_revision(PACKAGE, 1))
    assert prepared.status == "PREPARED"
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
    store, _, _ = subject
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
    prepared = await store.prepare(PACKAGE, canonical_revision(PACKAGE, 1))
    assert prepared.record is not None

    source.value = source.value.model_copy(update={
        "excluded_effects": (*source.value.excluded_effects, "credential changes")
    })
    changed = await store.submit(
        prepared.record.consequence_id, PACKAGE, prepared.record.package_revision, "APPROVED"
    )
    assert changed.status == "STALE" and changed.reason == "consequence_changed"
    async with engine.connect() as connection:
        row = (await connection.execute(human_review_consequences.select())).mappings().one()
    assert row["decision"] is None and row["state"] == "PENDING"

    source.value = source.value.model_copy(update={
        "package_revision": canonical_revision(PACKAGE, 2)
    })
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).where(
            canonical_work.c.work_id == PACKAGE
        ).values(row_version=2))
    stale = await store.submit(
        prepared.record.consequence_id, PACKAGE, prepared.record.package_revision, "APPROVED"
    )
    assert stale.status == "STALE" and stale.reason == "package_revision_changed"


async def test_prepare_and_submit_replay_are_exact_and_concurrent(subject):
    store, _, engine = subject
    revision = canonical_revision(PACKAGE, 1)
    left, right = await asyncio.gather(
        store.prepare(PACKAGE, revision), store.prepare(PACKAGE, revision)
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
    mismatch = await store.prepare(PACKAGE, canonical_revision(PACKAGE, 1))
    assert mismatch.status == "STALE" and mismatch.reason == "consequence_changed"
