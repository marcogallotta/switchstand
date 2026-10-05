import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.canonical_relations import (
    CanonicalRelationsRepository,
    project_memberships,
    projects,
    work_dependencies,
)
from switchstand.canonical_work import (
    CanonicalWorkRepository,
    canonical_metadata,
    canonical_revision,
    canonical_work,
)
from switchstand.priority_claim_service import PriorityClaimService
from switchstand.priority_claims import (
    ClaimKind,
    NewPriorityClaim,
    PriorityClaimRepository,
    RelationKind,
)
from switchstand.priority_context import PriorityContextProjection


@pytest.fixture
async def projection(database_prerequisite):
    del database_prerequisite
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    work_ids = tuple(uuid4() for _ in range(3))
    project_id = uuid4()
    async with engine.begin() as connection:
        await connection.run_sync(canonical_metadata.drop_all)
        await connection.run_sync(canonical_metadata.create_all)
        await connection.execute(canonical_work.insert(), [
            {
                "work_id": work_ids[0], "title": "Direct", "normalized_title": "direct",
                "completed": False, "notes": "ignored", "row_version": 1,
                "owner_key": "worker-a", "lifecycle_state": "CURRENT",
                "next_action_class": "IMPLEMENT", "next_action_ref": "code",
                "wait_kind": None, "unblock_condition": None,
            },
            {
                "work_id": work_ids[1], "title": "Blocked", "normalized_title": "blocked",
                "completed": False, "notes": "also ignored", "row_version": 1,
                "lifecycle_state": "WAITING", "wait_kind": "DEPENDENCY",
                "unblock_condition": "upstream lands",
                "owner_key": None, "next_action_class": None, "next_action_ref": None,
            },
            {
                "work_id": work_ids[2], "title": "Unranked", "normalized_title": "unranked",
                "completed": False, "notes": "headline-looking text", "row_version": 1,
                "owner_key": None, "lifecycle_state": None, "next_action_class": None,
                "next_action_ref": None, "wait_kind": None, "unblock_condition": None,
            },
        ])
        await connection.execute(projects.insert().values(project_id=project_id, name="Area"))
        await connection.execute(project_memberships.insert().values(
            project_id=project_id, work_id=work_ids[0], section_name="Doing",
        ))
        await connection.execute(work_dependencies.insert().values(
            work_id=work_ids[0], depends_on_work_id=work_ids[1],
        ))
    repository = PriorityClaimRepository(engine)
    projection = PriorityContextProjection(
        works=CanonicalWorkRepository(engine), relations=CanonicalRelationsRepository(engine),
        claims=PriorityClaimService(repository, CanonicalWorkRepository(engine)),
    )
    yield projection, repository, work_ids, project_id
    async with engine.begin() as connection:
        await connection.run_sync(canonical_metadata.drop_all)
    await engine.dispose()


def claim(
    kind: ClaimKind, subject: UUID, relation: RelationKind, *, project: bool = False, **values,
) -> NewPriorityClaim:
    return NewPriorityClaim(
        claim_id=uuid4(), claim_kind=kind, subject_kind="PROJECT" if project else "WORK",
        subject_id=subject, relation_kind=relation, rationale="governing test",
        source_label="HUMAN" if kind == "HUMAN_PRIORITY" else "AGENT",
        source_ref="test", **values,
    )


async def test_projection_is_bounded_deterministic_advisory_and_uncertain(projection):
    view, repository, work_ids, project_id = projection
    direct, blocked, unranked = work_ids
    await repository.record(claim("HUMAN_PRIORITY", direct, "BAND", band="HIGH"))
    await repository.record(claim(
        "AGENT_RECOMMENDATION", direct, "BEFORE", relation_target_id=blocked,
        source_observed_revision=canonical_revision(direct, 1),
    ))
    await repository.record(claim(
        "AGENT_RECOMMENDATION", direct, "BEFORE", relation_target_id=unranked,
        source_observed_revision=canonical_revision(direct, 1),
    ))
    await repository.record(claim("HUMAN_PRIORITY", project_id, "HOLD", project=True))
    await repository.record(claim(
        "AGENT_RECOMMENDATION", blocked, "BAND", band="NORMAL",
        source_observed_revision=canonical_revision(blocked, 2),
    ))

    result = await view.project((blocked, direct, direct))
    assert result.scope_complete and not result.portfolio_complete and result.next_cursor is None
    assert [row.work_id for row in result.rows] == sorted({direct, blocked}, key=str)
    rows = {row.work_id: row for row in result.rows}
    direct_row, blocked_row = rows[direct], rows[blocked]
    assert direct_row.priority_knowledge == "DIRECT_CURRENT"
    assert [(item.project_name, item.stage) for item in direct_row.projects] == [("Area", "Doing")]
    assert direct_row.dependency_work_ids == (blocked,)
    assert [item.authority for item in direct_row.work_claims] == ["HUMAN", "ADVISORY"]
    assert direct_row.project_claims[0].authority == "HUMAN"
    assert all(item.subject_kind == "PROJECT" for item in direct_row.project_claims)
    assert blocked_row.priority_knowledge == "CONFLICTING_OR_STALE"
    assert blocked_row.wait_kind == "DEPENDENCY" and "STALE_CLAIM" in blocked_row.flags
    assert {direct_row.coarse_size, direct_row.headline, direct_row.human_attention} == {"UNKNOWN"}
    assert not ({"score", "order", "frontier"} & set(direct_row.model_dump()))

    missing = uuid4()
    uncertain = await view.project((unranked, missing))
    uncertain_rows = {row.work_id: row for row in uncertain.rows}
    assert uncertain_rows[unranked].priority_knowledge == "UNKNOWN_UNRANKED"
    assert uncertain_rows[missing].state == "UNKNOWN"
    assert uncertain_rows[missing].flags == ("WORK_UNKNOWN",)


async def test_conflicting_human_bands_are_not_ranked(projection):
    view, repository, (_direct, _blocked, work_id), _project = projection
    await repository.record(claim("HUMAN_PRIORITY", work_id, "BAND", band="HIGH"))
    await repository.record(claim("HUMAN_PRIORITY", work_id, "BAND", band="NORMAL"))
    row = (await view.project((work_id,))).rows[0]
    assert row.priority_knowledge == "CONFLICTING_OR_STALE"
    assert row.flags == ("CONFLICTING_HUMAN_BAND",)
