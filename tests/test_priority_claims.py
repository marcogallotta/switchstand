import os
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.canonical_relations import projects
from switchstand.canonical_work import canonical_metadata, canonical_work
from switchstand.priority_claims import (
    NewPriorityClaim,
    PriorityClaimRepository,
    priority_claims,
)


@pytest.fixture
async def repository(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    work, target, project = uuid4(), uuid4(), uuid4()
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: canonical_metadata.create_all(
            sync, tables=[canonical_work, projects, priority_claims], checkfirst=True,
        ))
        await connection.execute(canonical_work.insert(), [{
            "work_id": value, "title": str(value), "normalized_title": str(value),
            "completed": False, "notes": "", "priority": "P0", "row_version": 1,
        } for value in (work, target)])
        await connection.execute(projects.insert().values(project_id=project, name="Project"))
    yield PriorityClaimRepository(engine), work, target, project
    async with engine.begin() as connection:
        await connection.execute(priority_claims.delete())
        await connection.execute(projects.delete().where(projects.c.project_id == project))
        await connection.execute(canonical_work.delete().where(
            canonical_work.c.work_id.in_((work, target))
        ))
        await connection.run_sync(lambda sync: priority_claims.drop(sync, checkfirst=True))
    await engine.dispose()
def claim(kind, subject, relation, **values):
    return NewPriorityClaim(
        claim_id=uuid4(), claim_kind="HUMAN_PRIORITY",
        subject_kind=kind, subject_id=subject, relation_kind=relation,
        rationale="current human direction", source_label="Marco", source_ref="message:1",
        **values,
    )
async def test_valid_shapes_and_bounded_reads(repository):
    repo, work, target, project = repository
    band = await repo.record(claim(
        "WORK", work, "BAND", band="HIGH",
    ))
    hold = await repo.record(claim("PROJECT", project, "HOLD"))
    before = await repo.record(claim(
        "WORK", work, "BEFORE", relation_target_id=target,
    ))
    assert await repo.current("WORK", work) == (band, before)
    assert await repo.current("PROJECT", project) == (hold,)
    assert await repo.before_edges("WORK", (work, target)) == (before,)
    assert await repo.before_edges("WORK", ()) == ()
    assert band.created_at is not None


async def test_invalid_shapes_and_targets_roll_back(repository):
    repo, work, _target, project = repository
    with pytest.raises(LookupError, match="work subject"):
        await repo.record(claim("WORK", uuid4(), "HOLD"))
    with pytest.raises(LookupError, match="work subject"):
        await repo.record(claim(
            "WORK", work, "BEFORE", relation_target_id=project,
        ))
    with pytest.raises(ValueError, match="precede itself"):
        await repo.record(claim(
            "WORK", work, "BEFORE", relation_target_id=work,
        ))
    for invalid in (
        claim("WORK", work, "BAND"),
        claim("WORK", work, "HOLD", band="NORMAL"),
        claim("WORK", work, "BEFORE"),
    ):
        with pytest.raises(IntegrityError):
            await repo.record(invalid)
    assert await repo.current("WORK", work) == ()


async def test_supersession_preserves_history_and_legacy_priority(repository):
    repo, work, target, project = repository
    first = await repo.record(claim(
        "WORK", work, "BAND", band="NORMAL",
    ))
    second = await repo.record(claim(
        "WORK", work, "HOLD", supersedes_claim_id=first.claim_id,
    ))
    assert await repo.current("WORK", work) == (second,)
    history = await repo.provenance(second.claim_id)
    assert [value.claim_id for value in history] == [second.claim_id, first.claim_id]
    assert [value.state for value in history] == ["CURRENT", "SUPERSEDED"]
    with pytest.raises(ValueError, match="not current"):
        await repo.record(claim(
            "WORK", work, "HOLD", supersedes_claim_id=first.claim_id,
        ))
    async with repo.engine.connect() as connection:
        assert await connection.scalar(select(canonical_work.c.priority).where(
            canonical_work.c.work_id == work
        )) == "P0"
    assert await repo.current("PROJECT", project) == ()
    assert await repo.before_edges("WORK", (work, target), limit=1) == ()
