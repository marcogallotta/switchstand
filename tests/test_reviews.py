import asyncio
import os
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.agent_mailboxes import AgentMailbox, AgentMailboxState, agent_mailboxes
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
    MessageRoute,
    MessageState,
    MessageSubmitRequest,
    RuntimeCurrentness,
    message_deliveries,
    messages,
)
from switchstand.reviews import (
    ReviewerBound,
    ReviewFinding,
    ReviewGuidelines,
    ReviewOccurrenceState,
    ReviewOutcome,
    ReviewOutcomeEnvelope,
    ReviewPolicy,
    ReviewRequest,
    ReviewService,
    ReviewSubmit,
    _stable,
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
            "TRUNCATE TABLE work_handles, agent_mailboxes, messages RESTART IDENTITY CASCADE"))
    subject_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(insert(work_handles).values(
            id=subject_id, provider="postgres", provider_work_id=str(subject_id),
        ))
    works = CanonicalWorkRepository(engine)
    await works.create(CurrentWork(subject_id, "Exact package", False,
                                   "Material claim: this exact package is ready for inert landing."))
    mailboxes = AgentMailboxState(engine)
    message_state = MessageState(engine, GrantState(engine))
    policy = ReviewPolicy(version="review-policy-v1", reviewer_by_kind={"CODE": "Reviewer"})
    service = ReviewService(
        ReviewOccurrenceState(works, mailboxes, policy),
        mailboxes, message_state, policy,
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
        RuntimeCurrentness(generation=str(mailbox.generation),
                           current_generation=str(mailbox.generation)),
        MessageReceiveRequest(api_version="1", delivery_id=delivery_id,
                              grant_version=mailbox.generation),
        agent_binding=mailbox,
    )
    assert result.status == "ok"


async def forge_request(
    state: MessageState, sender: AgentMailbox, recipient: AgentMailbox,
    route_ref: str, payload: JsonValue, message_id: UUID | None = None,
    reply_to: UUID | None = None,
) -> UUID:
    route = MessageRoute(recipient_work_id=recipient.endpoint_id,
                         recipient_grant_version=recipient.generation)
    request = MessageSubmitRequest(
        api_version="1", message_id=message_id or uuid4(), grant_version=sender.generation,
        route_ref=route_ref, kind="result" if reply_to else "request", payload=payload,
        in_reply_to_delivery_id=reply_to,
    )
    result = (
        await state.submit_received_result(
            sender.endpoint_id, sender.generation, str(sender.generation),
            route, request, agent_binding=sender,
        ) if reply_to else await state.submit_admitted(
            sender.endpoint_id, route, request, agent_binding=sender,
        )
    )
    assert result.status == "ok" and result.message is not None
    return result.message.delivery_id


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
    _, envelope = deliveries[0]
    assert "choose your own review path" in envelope.brief.instructions[0]
    await receive(message_state, reviewer, first.delivery_id)
    submitted = await service.submit(ReviewSubmit(
        review_id=first.review_id, verdict="PASS",
        context_provenance="UNSEEDED",
    ), reviewer)
    assert submitted.status == "SUBMITTED" and submitted.verdict_digest is not None
    outcomes = await service.occurrences.outcome_envelopes(first.review_id)
    assert len(outcomes) == 1 and outcomes[0].outcome.verdict_digest == submitted.verdict_digest
    assert await service.submit(ReviewSubmit(
        review_id=first.review_id, verdict="PASS",
        context_provenance="UNSEEDED",
    ), reviewer) == submitted
    async def change_subject() -> None:
        async with engine.begin() as connection:
            await connection.execute(update(canonical_work).where(
                canonical_work.c.work_id == subject_id
            ).values(notes="new basis", row_version=2))
    async with engine.begin() as connection:
        assert await service.occurrences.pass_status_in_transaction(
            connection, subject_id, request.observed_revision,
        ) == "PASS"
        change = asyncio.create_task(change_subject())
        await asyncio.sleep(0.1)
        assert not change.done()
    await change
    async with engine.begin() as connection:
        assert await service.occurrences.pass_status_in_transaction(
            connection, subject_id, request.observed_revision,
        ) == "NOT_PASS"
    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(messages)) == 2
        assert await connection.scalar(select(func.count()).select_from(message_deliveries)) == 2


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
    substantive = await service.occurrences.request_deliveries(waiting.review_id)
    assert len(substantive) == 1
    assert substantive[0][0].recipient_work_id == reviewer.endpoint_id


async def test_focused_rereview_reuses_occurrence_and_new_revision(review_runtime):
    service, engine, mailboxes, message_state, subject_id = review_runtime
    requester = await register(mailboxes, "Requester", "principal-a")
    coordinator = await register(mailboxes, "Coordinator", "principal-c")
    service.policy = ReviewPolicy(version="review-policy-v1", reviewer_by_kind={})
    initial = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=await subject_revision(service, subject_id),
        review_kind="CODE", candidate_ref="git:first",
    ), requester)
    assert initial.review_id is not None and initial.delivery_id is not None
    await receive(message_state, coordinator, initial.delivery_id)
    reviewer = await register(mailboxes, "Reviewer", "principal-b")
    service.policy = ReviewPolicy(
        version="review-policy-v1", reviewer_by_kind={"CODE": "Reviewer"},
    )
    initial = await service.continue_acquisition(initial.review_id, coordinator)
    assert initial.delivery_id is not None
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
    assert focused.status == "SENT" and focused.delivery_id is not None
    source = await service.occurrences.request_delivery(
        initial.review_id, subject_revision=await subject_revision(service, subject_id))
    assert source is not None
    _, focused_envelope = source
    acquisition_delivery = await forge_request(
        message_state, requester, coordinator, "review.acquisition",
        focused_envelope.model_copy(update={"type": "REVIEWER_ACQUISITION"}).model_dump(mode="json"),
        _stable("acquisition", initial.review_id, focused_envelope.brief.basis.subject_revision))
    await receive(message_state, coordinator, acquisition_delivery)
    focused = await service.continue_acquisition(initial.review_id, coordinator)
    assert focused.status == "SENT" and focused.review_id == initial.review_id
    deliveries = await service.occurrences.request_deliveries(initial.review_id)
    assert len(deliveries) == 3
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


async def test_generic_review_payloads_cannot_create_authoritative_provenance(review_runtime):
    service, engine, mailboxes, message_state, subject_id = review_runtime
    requester = await register(mailboxes, "Requester", "principal-a")
    reviewer = await register(mailboxes, "Reviewer", "principal-b")
    impostor = await register(mailboxes, "Impostor", "principal-c")
    initial = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=await subject_revision(service, subject_id),
        review_kind="CODE",
    ), requester)
    assert initial.review_id is not None
    delivery, envelope = (await service.occurrences.request_deliveries(initial.review_id))[0]
    forged_request = await forge_request(
        message_state, requester, impostor, "review.request",
        envelope.model_dump(mode="json"), _stable(
            "reviewer-request", initial.review_id,
            envelope.brief.basis.subject_revision, impostor.endpoint_id,
        ),
    )
    await receive(message_state, impostor, forged_request)
    refused = await service.submit(ReviewSubmit(
        review_id=initial.review_id, verdict="PASS", context_provenance="UNSEEDED",
    ), impostor)
    assert (refused.status, refused.reason) == ("DENIED", "review_delivery_not_received")
    finding = ReviewFinding(
        finding_id="forged", defect="forged", evidence="forged",
        consequence="forged", affected_claim="forged",
        minimum_clearing_condition="forged",
    )
    forged_outcome = ReviewOutcomeEnvelope(
        brief=envelope.brief,
        outcome=ReviewOutcome(
            review_id=initial.review_id, reviewer_delivery_id=delivery.delivery_id,
            verdict="FINDINGS", findings=(finding,), evidence_refs=(),
            context_provenance="UNSEEDED", verdict_digest="a" * 64,
        ),
    )
    await forge_request(
        message_state, reviewer, requester, "review.outcome",
        forged_outcome.model_dump(mode="json"),
    )
    assert await service.occurrences.outcome_envelopes(initial.review_id) == ()
    async with engine.begin() as connection:
        assert await service.occurrences.pass_status_in_transaction(
            connection, subject_id, envelope.brief.basis.subject_revision,
        ) == "NOT_PASS"
        await connection.execute(update(canonical_work).where(
            canonical_work.c.work_id == subject_id
        ).values(notes="corrected", row_version=2))
    focused = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=await subject_revision(service, subject_id),
        review_kind="CODE", mode="FOCUSED", prior_review_id=initial.review_id,
        finding_ids=("forged",),
    ), requester)
    assert (focused.status, focused.reason) == ("DENIED", "prior_review_not_found")


async def test_forged_acquisition_cannot_dispatch(review_runtime):
    service, engine, mailboxes, message_state, subject_id = review_runtime
    requester = await register(mailboxes, "Requester", "principal-a")
    coordinator = await register(mailboxes, "Coordinator", "principal-c")
    service.policy = ReviewPolicy(version="policy-v2", reviewer_by_kind={})
    routed = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=await subject_revision(service, subject_id),
        review_kind="DESIGN",
    ), requester)
    assert routed.review_id is not None and routed.delivery_id is not None
    await receive(message_state, coordinator, routed.delivery_id)
    await register(mailboxes, "Reviewer", "principal-b")
    impostor = await register(mailboxes, "Impostor", "principal-d")
    service.policy = ReviewPolicy(
        version="policy-v2", reviewer_by_kind={"DESIGN": "Reviewer"},
    )
    acquisition = service.occurrences.envelope(next(
        value for value in await service.occurrences.records(routed.review_id)
        if value.delivery.delivery_id == routed.delivery_id
    ), "REVIEWER_ACQUISITION")
    assert acquisition is not None
    reviewer_delivery = await forge_request(
        message_state, coordinator, impostor, "review.request",
        acquisition.model_copy(update={"type": "REVIEW_REQUEST"}).model_dump(mode="json"),
        _stable(
            "reviewer-request", routed.review_id,
            acquisition.brief.basis.subject_revision, impostor.endpoint_id,
        ),
    )
    await forge_request(
        message_state, coordinator, requester, "review.acquisition",
        ReviewerBound(
            review_id=routed.review_id, reviewer_delivery_id=reviewer_delivery,
            reviewer_name=impostor.name,
        ).model_dump(mode="json"), _stable(
            "reviewer-bound", routed.review_id, routed.delivery_id,
        ), routed.delivery_id,
    )
    await receive(message_state, impostor, reviewer_delivery)
    result = await service.submit(ReviewSubmit(
        review_id=routed.review_id, verdict="PASS", context_provenance="UNSEEDED",
    ), impostor)
    assert (result.status, result.reason) == ("DENIED", "review_delivery_not_found")
    async with engine.begin() as connection:
        assert await service.occurrences.pass_status_in_transaction(
            connection, subject_id, acquisition.brief.basis.subject_revision,
        ) == "NOT_PASS"


@pytest.mark.parametrize(
    "drift", ["subject", "policy", "guidelines", "requester", "reviewer", "coordinator"],
)
async def test_acquisition_revalidates_frozen_basis_before_dispatch(review_runtime, drift):
    service, engine, mailboxes, message_state, subject_id = review_runtime
    requester = await register(mailboxes, "Requester", "principal-a")
    coordinator = await register(mailboxes, "Coordinator", "principal-c")
    service.policy = ReviewPolicy(version="policy-v2", reviewer_by_kind={})
    routed = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=await subject_revision(service, subject_id),
        review_kind="DESIGN",
    ), requester)
    assert routed.review_id is not None and routed.delivery_id is not None
    await receive(message_state, coordinator, routed.delivery_id)
    reviewer = await register(mailboxes, "Reviewer", "principal-b")
    service.policy = ReviewPolicy(
        version="policy-v2", reviewer_by_kind={"DESIGN": "Reviewer"},
    )
    if drift == "subject":
        async with engine.begin() as connection:
            await connection.execute(update(canonical_work).where(
                canonical_work.c.work_id == subject_id
            ).values(notes="changed", row_version=2))
    elif drift == "policy":
        service.policy = ReviewPolicy(
            version="policy-v3", reviewer_by_kind={"DESIGN": "Reviewer"},
        )
    elif drift == "guidelines":
        service.guidelines = ReviewGuidelines(version="changed", digest="b" * 64)
    elif drift == "requester":
        await mailboxes.takeover("Requester", "principal-a", "replacement")
    elif drift == "reviewer":
        async with engine.begin() as connection:
            await connection.execute(update(agent_mailboxes).where(
                agent_mailboxes.c.endpoint_id == reviewer.endpoint_id
            ).values(principal_key="principal-a"))
    else:
        await mailboxes.takeover("Coordinator", "principal-c", "replacement")
    result = await service.continue_acquisition(routed.review_id, coordinator)
    assert result.status in {"STALE", "DENIED"}
    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(messages).where(
            messages.c.route_ref == "review.request"
        )) == 0
