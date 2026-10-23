import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import bcrypt
import httpx
import pytest
from chatgpt_fixture import PRINCIPAL, service
from sqlalchemy import insert, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand import chatgpt_mcp
from switchstand.agent_mailboxes import AgentMailbox, AgentMailboxResult
from switchstand.canonical_work import (
    CanonicalWorkRepository,
    canonical_metadata,
    canonical_revision,
    canonical_work,
)
from switchstand.human_review_shell import create_human_review_shell
from switchstand.human_reviews import (
    HumanReviewCandidate,
    HumanReviewConsequence,
    HumanReviewState,
    approved_baseline_in_transaction,
    human_review_consequences,
)
from switchstand.repository_candidate import RepositoryCandidateQualification

PACKAGE = UUID("60000000-0000-4000-8000-000000000001")


@pytest.mark.parametrize("decision", ["APPROVED", "NO_DISPATCH"])
async def test_ordinary_agent_consumes_separate_candidate_decision(subject, monkeypatch, decision):
    store, source, engine = subject
    task = v2_consequence(source.value.package_revision)
    await store.propose(task)
    await store.submit(task.consequence_id, PACKAGE, task.package_revision, "APPROVED")
    mailbox = AgentMailbox(name="Builder", name_key="builder", endpoint_id=uuid4(),
        principal_key=PRINCIPAL.key, session_key="session", generation=1)
    monkeypatch.setattr(chatgpt_mcp, "AgentMailboxState", lambda _: SimpleNamespace(
        for_actor=AsyncMock(return_value=AgentMailboxResult(status="ok", mailbox=mailbox))))
    application = service()
    application.messages = SimpleNamespace(engine=engine)
    application.human_reviews = store
    application.human_review_origin = "https://review.example.test"
    tools = dict(chatgpt_mcp.build_ordinary_tools(application, agent_identity=lambda: "session"))
    candidate = HumanReviewCandidate(task_approval_id=task.consequence_id,
        pull_request=7, base_sha="a" * 40, head_sha="b" * 40, composition_sha="c" * 40)
    quality = RepositoryCandidateQualification(status="READY", pull_request=7, gates=[],
        base_sha=candidate.base_sha, head_sha=candidate.head_sha,
        composition_sha=candidate.composition_sha)
    monkeypatch.setattr(chatgpt_mcp.repository_candidate, "qualify_repository_candidate",
        AsyncMock(return_value=quality))
    qualify = tools["repository_candidate_qualification_get"]
    assert (await qualify("1", 7, work_id=PACKAGE)).reason == "candidate_approval_required"
    request = HumanReviewConsequence(package_work_id=PACKAGE, package_revision=task.package_revision,
        implementation_scope=("Merge the independently reviewed exact code candidate",),
        implementation_target="repository main", excluded_effects=("activation",),
        approval_effect="APPROVE_EXACT_CANDIDATE_FOR_LANDING", candidate=candidate)
    created = await tools["human_review_create"]("1", request)
    identity = UUID(created["record"]["consequence_id"])
    assert "consequence_id=" in created["url"]
    assert (await qualify("1", 7)).reason == "candidate_approval_pending"
    assert (await store.prepare(PACKAGE, task.package_revision)).record.consequence_id == task.consequence_id
    browser = create_human_review_shell(store, expected_origin=application.human_review_origin,
        password_hash=bcrypt.hashpw(b"human password", bcrypt.gensalt(rounds=4)).decode())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=browser),
        base_url=application.human_review_origin) as human:
        assert (await human.post(created["url"], data={"password": "human password"},
            headers={"Origin": application.human_review_origin})).status_code == 200
        page = await human.get(created["url"])
        assert "Approve exact code candidate for merge" in page.text
        assert candidate.head_sha in page.text
        response = await human.post("/human-review/submit", data={
            "consequence_id": str(identity), "package_work_id": str(PACKAGE),
            "package_revision": task.package_revision,
            "consequence_digest": created["record"]["consequence_digest"], "decision": decision,
        }, headers={"Origin": application.human_review_origin})
        assert response.status_code == 200
    saved = await tools["human_review_get"]("1", PACKAGE, task.package_revision, identity)
    assert saved["record"]["decision"] == decision
    result = await qualify("1", 7)
    if decision != "APPROVED":
        assert result.status == "NOT_READY" and result.reason == "candidate_approval_rejected"
        return
    assert result.status == "READY"
    for field in ("base_sha", "head_sha", "composition_sha"):
        monkeypatch.setattr(chatgpt_mcp.repository_candidate, "qualify_repository_candidate",
            AsyncMock(return_value=quality.model_copy(update={field: "d" * 40})))
        assert (await qualify("1", 7)).reason == "candidate_changed"
    monkeypatch.setattr(chatgpt_mcp.repository_candidate, "qualify_repository_candidate",
        AsyncMock(return_value=quality))
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).values(notes="new agent notes", row_version=2))
    assert (await qualify("1", 7)).status == "READY"
    replacement = v2_consequence(canonical_revision(PACKAGE, 2), supersedes=task.consequence_id)
    await store.propose(replacement)
    await store.submit(replacement.consequence_id, PACKAGE, replacement.package_revision, "APPROVED")
    assert (await qualify("1", 7)).reason == "task_approval_changed"
    assert (await tools["human_review_get"](
        "1", PACKAGE, task.package_revision, identity))["status"] == "STALE"
    renewed = request.model_copy(update={"package_revision": replacement.package_revision,
        "candidate": candidate.model_copy(update={"task_approval_id": replacement.consequence_id})})
    created = await tools["human_review_create"]("1", renewed)
    assert (await qualify("1", 7)).reason == "candidate_approval_pending"
    await store.submit(UUID(created["record"]["consequence_id"]), PACKAGE,
        replacement.package_revision, "APPROVED")
    assert (await qualify("1", 7)).status == "READY"


class ConsequenceSource:
    def __init__(self, revision: str):
        self.value = HumanReviewConsequence(
            package_work_id=PACKAGE,
            package_revision=revision,
            implementation_scope=("Implement the reviewed package",),
            implementation_target="repository main through normal reviewed landing",
            excluded_effects=("deployment", "activation", "provider production writes"),
        )


def v2_consequence(
    revision: str, *, supersedes: UUID | None = None,
    effects: tuple[str, ...] = ("IMPLEMENT", "LAND"),
) -> HumanReviewConsequence:
    return HumanReviewConsequence(
        package_work_id=PACKAGE,
        package_revision=revision,
        implementation_scope=("Implement and land the approved package",),
        implementation_target="repository main",
        excluded_effects=("activation", "deployment"),
        approval_effect="CLEAR_HUMAN_REVIEW_FOR_NAMED_CONTINUATION",
        baseline={
            "human_source_refs": ("work:5998b00d",),
            "opening": {
                "what_you_asked": "Ship the behavior", "what_this_does": "Implement the package",
                "what_this_does_not_or_changes_for_you": "Does not activate it",
                "size": "Small repository change",
            },
            "approved_meaning": ("The approved behavior is durable",),
            "protected_requirements": ("Preserve the reviewed outcome",),
            "approved_non_goals": ("No activation",),
            "decision_context": {
                "size_or_burden": ("One implementation PR",), "scale_assumptions": (),
                "material_risks_and_boundaries": ("Activation remains separate",),
            },
            "continuation_effects": effects,
            "supersedes_baseline_id": supersedes,
        },
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
    selected = await store.prepare(
        PACKAGE, source.value.package_revision, prepared.record.consequence_id,
    )
    submit = await store.submit(
        prepared.record.consequence_id, PACKAGE, source.value.package_revision, "APPROVED"
    )
    assert read.status == selected.status == submit.status == "UNKNOWN"
    assert read.reason == submit.reason == "state_unavailable"


async def test_v2_supersession_is_explicit_append_only_and_current(subject):
    store, _, engine = subject
    first = v2_consequence(canonical_revision(PACKAGE, 1))
    prepared = await store.propose(first)
    assert prepared.record is not None
    await store.submit(prepared.record.consequence_id, PACKAGE, first.package_revision, "APPROVED")

    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).where(
            canonical_work.c.work_id == PACKAGE
        ).values(row_version=2))
    revision = canonical_revision(PACKAGE, 2)
    missing_reference = await store.propose(v2_consequence(revision))
    assert (missing_reference.status, missing_reference.reason) == (
        "DENIED", "baseline_supersession_required",
    )

    successor = v2_consequence(revision, supersedes=first.consequence_id)
    proposed = await store.propose(successor)
    assert proposed.status == "PREPARED" and proposed.record is not None
    approved = await store.submit(
        proposed.record.consequence_id, PACKAGE, revision, "APPROVED"
    )
    assert approved.status == "RECORDED"

    async with engine.connect() as connection:
        baseline = await approved_baseline_in_transaction(connection, first.consequence_id, PACKAGE)
        rows = (await connection.execute(
            human_review_consequences.select().order_by(human_review_consequences.c.created_at)
        )).mappings().all()
    assert baseline is not None and baseline.consequence.baseline == first.baseline
    assert len(rows) == 2
    assert rows[0]["consequence_id"] == first.consequence_id
    assert rows[1]["consequence_id"] == successor.consequence_id


def test_v2_closed_shape_rejects_effect_exclusion():
    revision = canonical_revision(PACKAGE, 1)
    with pytest.raises(ValueError, match="also excluded"):
        v2_consequence(revision, effects=("IMPLEMENT", "ACTIVATE"))


async def test_task_enrollment_utf8_limit_and_legacy_digest(subject):
    store, _, engine = subject
    legacy = v2_consequence(canonical_revision(PACKAGE, 1))
    assert legacy.digest == "696090832a34674142e1f04f070356363c40dab422b6abb2b8da670228154c2c"
    task = legacy.model_copy(update={"task_text": "Finish the still unfinished Task"})
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).values(notes="é" * 1024 + "x"))
    assert (await store.propose(task)).status == "PREPARED"
    assert (await store.submit(task.consequence_id, PACKAGE, task.package_revision,
                               "APPROVED")).reason == "agent_notes_overflow"
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).values(notes="é" * 1024, row_version=2))
    assert (await store.submit(task.consequence_id, PACKAGE, task.package_revision,
                               "APPROVED")).status == "STALE"
    task = task.model_copy(update={"package_revision": canonical_revision(PACKAGE, 2)})
    assert (await store.propose(task)).status == "PREPARED"
    assert (await store.submit(task.consequence_id, PACKAGE, task.package_revision,
                               "APPROVED")).status == "RECORDED"
