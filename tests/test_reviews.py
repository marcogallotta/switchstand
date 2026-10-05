import os
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.agent_mailboxes import AgentMailbox, AgentMailboxState
from switchstand.canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_metadata,
    canonical_revision,
    canonical_work,
)
from switchstand.grant_state import GrantState
from switchstand.messages import (
    MessageReceiveRequest,
    MessageState,
    RuntimeCurrentness,
    message_deliveries,
    messages,
)
from switchstand.reviews import (
    ReviewFinding,
    ReviewGuidelines,
    ReviewOccurrenceState,
    ReviewPolicy,
    ReviewRequest,
    ReviewService,
    ReviewSubmit,
)
from switchstand.state import work_handles

GUIDELINES_DIGEST = "f34b4c5f5c715a0908c80b4adbd6040bf071652f43c41e5a59b448f9b7c2a9ee"


@pytest.fixture
async def review_runtime(database_prerequisite) -> AsyncGenerator[
    tuple[ReviewService, AsyncEngine, AgentMailboxState, MessageState, UUID]
]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(canonical_metadata.create_all)
        await connection.execute(text(
            "TRUNCATE TABLE work_handles, agent_mailboxes, messages "
            "RESTART IDENTITY CASCADE"
        ))
    subject_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(insert(work_handles).values(
            id=subject_id, provider="postgres", provider_work_id=str(subject_id),
        ))
    works = CanonicalWorkRepository(engine)
    await works.create(CurrentWork(
        subject_id, "Exact package", False,
        "Material claim: this exact package is ready for inert landing.",
    ))
    mailboxes = AgentMailboxState(engine)
    message_state = MessageState(engine, GrantState(engine))
    service = ReviewService(
        ReviewOccurrenceState(works, message_state), mailboxes, message_state,
        ReviewPolicy(version="review-policy-v1", reviewer_by_kind={"CODE": "Reviewer"}),
        ReviewGuidelines(
            version="code-quality@f2b7d79d3255c27247250b71f3db966513b02234",
            digest=GUIDELINES_DIGEST,
        ),
    )
    yield service, engine, mailboxes, message_state, subject_id
    async with engine.begin() as connection:
        await connection.execute(text(
            "TRUNCATE TABLE work_handles, agent_mailboxes, messages "
            "RESTART IDENTITY CASCADE"
        ))
        await connection.run_sync(canonical_metadata.drop_all)
    await engine.dispose()


async def register(
    mailboxes: AgentMailboxState, name: str, principal: str,
) -> AgentMailbox:
    result = await mailboxes.register_agent(name, principal, f"chat-{name}")
    assert result.status == "ok" and result.mailbox is not None
    return result.mailbox


async def subject_revision(service: ReviewService, subject_id: UUID) -> str:
    subject = await service.works.get(subject_id)
    assert subject is not None
    return canonical_revision(subject.work_id, subject.row_version)


async def receive(
    message_state: MessageState, mailbox: AgentMailbox, delivery_id: UUID,
) -> None:
    result = await message_state.receive_admitted(
        mailbox.endpoint_id, mailbox.generation,
        RuntimeCurrentness(
            generation=str(mailbox.generation),
            current_generation=str(mailbox.generation),
        ),
        MessageReceiveRequest(
            api_version="1", delivery_id=delivery_id,
            grant_version=mailbox.generation,
        ),
        agent_binding=mailbox,
    )
    assert result.status == "ok"


async def test_direct_review_is_server_briefed_recoverable_and_submitted_once(review_runtime):
    service, engine, mailboxes, message_state, subject_id = review_runtime
    requester = await register(mailboxes, "Requester", "principal-a")
    reviewer = await register(mailboxes, "Reviewer", "principal-b")
    request = ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=await subject_revision(service, subject_id),
        review_kind="CODE", candidate_ref="git:0123456789abcdef",
    )

    first = await service.request(request, requester)
    replay = await service.request(request, requester)
    assert first == replay
    assert first.status == "SENT" and first.delivery_id is not None
    assert first.review_id is not None

    deliveries = await service.occurrences.request_deliveries(first.review_id)
    assert len(deliveries) == 1
    delivery, envelope = deliveries[0]
    assert envelope.brief.basis.policy_version == "review-policy-v1"
    assert envelope.brief.basis.guidelines_digest == GUIDELINES_DIGEST
    assert envelope.brief.material_claim.startswith("Material claim:")
    assert "choose your own review path" in envelope.brief.instructions[0]
    assert envelope.brief.named_evidence == ("git:0123456789abcdef",)
    assert delivery.recipient_work_id == reviewer.endpoint_id

    await receive(message_state, reviewer, first.delivery_id)
    submitted = await service.submit(ReviewSubmit(
        review_id=first.review_id, verdict="PASS",
        context_provenance="UNSEEDED",
    ), reviewer)
    assert submitted.status == "SUBMITTED"
    assert submitted.verdict_digest is not None
    outcomes = await service.occurrences.outcomes(first.review_id)
    assert len(outcomes) == 1
    assert outcomes[0].verdict == "PASS"
    assert outcomes[0].verdict_digest == submitted.verdict_digest
    assert await service.submit(ReviewSubmit(
        review_id=first.review_id, verdict="PASS",
        context_provenance="UNSEEDED",
    ), reviewer) == submitted

    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(messages)) == 2
        assert await connection.scalar(
            select(func.count()).select_from(message_deliveries)
        ) == 2


async def test_policy_not_mailbox_name_controls_eligibility(review_runtime):
    service, _engine, mailboxes, _message_state, subject_id = review_runtime
    requester = await register(mailboxes, "Requester", "same-principal")
    await register(mailboxes, "Reviewer", "same-principal")
    result = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=await subject_revision(service, subject_id),
        review_kind="CODE",
    ), requester)
    assert (result.status, result.reason) == ("DENIED", "reviewer_not_eligible")


async def test_missing_reviewer_routes_exact_coordinator_then_same_occurrence(review_runtime):
    service, _engine, mailboxes, message_state, subject_id = review_runtime
    requester = await register(mailboxes, "Requester", "principal-a")
    service.policy = ReviewPolicy(version="policy-v2", reviewer_by_kind={})
    request = ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=await subject_revision(service, subject_id),
        review_kind="DESIGN",
    )
    waiting = await service.request(request, requester)
    assert (waiting.status, waiting.reason) == (
        "WAITING_REVIEWER", "coordinator_not_registered",
    )
    assert waiting.review_id is not None

    coordinator = await register(mailboxes, "Coordinator", "principal-c")
    routed = await service.request(request, requester)
    assert routed.review_id == waiting.review_id and routed.delivery_id is not None
    assert routed.status == "WAITING_REVIEWER"
    await receive(message_state, coordinator, routed.delivery_id)

    reviewer = await register(mailboxes, "Reviewer", "principal-b")
    service.policy = ReviewPolicy(
        version="policy-v2", reviewer_by_kind={"DESIGN": "Reviewer"},
    )
    continued = await service.continue_acquisition(waiting.review_id, coordinator)
    assert continued.status == "SENT" and continued.delivery_id is not None
    assert (await service.works.get(waiting.review_id)) is not None
    substantive = await service.occurrences.request_deliveries(waiting.review_id)
    assert len(substantive) == 1
    assert substantive[0][0].recipient_work_id == reviewer.endpoint_id


async def test_focused_rereview_reuses_occurrence_and_new_revision(review_runtime):
    service, engine, mailboxes, message_state, subject_id = review_runtime
    requester = await register(mailboxes, "Requester", "principal-a")
    reviewer = await register(mailboxes, "Reviewer", "principal-b")
    initial = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=await subject_revision(service, subject_id),
        review_kind="CODE", candidate_ref="git:first",
    ), requester)
    assert initial.review_id is not None and initial.delivery_id is not None
    await receive(message_state, reviewer, initial.delivery_id)
    finding = ReviewFinding(
        finding_id="F-1", defect="The claim is not established.",
        evidence="Boundary evidence is absent.", consequence="Landing is unsafe.",
        affected_claim="inert landing", minimum_clearing_condition="Add evidence.",
    )
    submitted = await service.submit(ReviewSubmit(
        review_id=initial.review_id, verdict="FINDINGS", findings=(finding,),
        context_provenance="UNSEEDED",
    ), reviewer)
    assert submitted.status == "SUBMITTED"
    service.policy = ReviewPolicy(
        version="changed-policy", reviewer_by_kind={"CODE": "Reviewer"},
    )
    changed_basis = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=await subject_revision(service, subject_id),
        review_kind="CODE", candidate_ref="git:second", mode="FOCUSED",
        prior_review_id=initial.review_id, finding_ids=("F-1",),
    ), requester)
    assert (changed_basis.status, changed_basis.reason) == (
        "DENIED", "prior_review_not_found",
    )
    service.policy = ReviewPolicy(
        version="review-policy-v1", reviewer_by_kind={"CODE": "Reviewer"},
    )
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).where(
            canonical_work.c.work_id == subject_id
        ).values(notes="Corrected material claim", row_version=2))
    focused = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=await subject_revision(service, subject_id),
        review_kind="CODE", candidate_ref="git:second", mode="FOCUSED",
        prior_review_id=initial.review_id, finding_ids=("F-1",),
    ), requester)
    assert focused.status == "SENT" and focused.review_id == initial.review_id
    deliveries = await service.occurrences.request_deliveries(initial.review_id)
    assert len(deliveries) == 2
    focused_brief = next(
        envelope.brief for _, envelope in deliveries
        if envelope.brief.basis.mode == "FOCUSED"
    )
    assert focused_brief.basis.finding_ids == ("F-1",)
    assert any("secondary/global-impact" in line for line in focused_brief.instructions)


async def test_subject_change_fails_closed_before_any_delivery(review_runtime):
    service, engine, mailboxes, _message_state, subject_id = review_runtime
    requester = await register(mailboxes, "Requester", "principal-a")
    await register(mailboxes, "Reviewer", "principal-b")
    stale = await subject_revision(service, subject_id)
    subject = await service.works.get(subject_id)
    assert subject is not None
    await service.works.replace(CurrentWork(
        subject.work_id, subject.title, subject.completed, "changed",
        row_version=subject.row_version,
    ))
    result = await service.request(ReviewRequest(
        subject_work_id=subject_id, observed_revision=stale, review_kind="CODE",
    ), requester)
    assert (result.status, result.reason) == ("STALE", "subject_revision_changed")
    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(messages)) == 0
