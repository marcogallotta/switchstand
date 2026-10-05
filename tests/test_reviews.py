import os
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import switchstand.state
from switchstand.agent_mailboxes import AgentMailboxState
from switchstand.canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_metadata,
    canonical_revision,
)
from switchstand.grant_state import GrantState
from switchstand.messages import MessageRoute, MessageState, MessageSubmitRequest
from switchstand.reviews import (
    CanonicalReviewBrief,
    ReviewBasis,
    ReviewEnvelope,
    ReviewGuidelines,
    ReviewOccurrenceState,
    ReviewPolicy,
    ReviewRequest,
    ReviewService,
    _stable,
)
from switchstand.state import work_handles


@pytest.fixture
async def occurrence_runtime(database_prerequisite) -> AsyncGenerator[
    tuple[ReviewOccurrenceState, MessageState, AgentMailboxState, UUID, AsyncEngine]
]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(switchstand.state.metadata.create_all)
        await connection.run_sync(canonical_metadata.create_all)
        await connection.execute(text(
            "TRUNCATE TABLE work_handles, agent_mailboxes, messages RESTART IDENTITY CASCADE"
        ))
    subject_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(insert(work_handles).values(
            id=subject_id, provider="postgres", provider_work_id=str(subject_id),
        ))
    works = CanonicalWorkRepository(engine)
    await works.create(CurrentWork(subject_id, "Exact package", False, "Material claim"))
    mailboxes = AgentMailboxState(engine)
    policy = ReviewPolicy(version="policy-v1", reviewer_by_kind={"CODE": "Reviewer"})
    yield ReviewOccurrenceState(works, mailboxes, policy), MessageState(
        engine, GrantState(engine)
    ), mailboxes, subject_id, engine
    async with engine.begin() as connection:
        await connection.execute(text(
            "TRUNCATE TABLE work_handles, agent_mailboxes, messages RESTART IDENTITY CASCADE"
        ))
        await connection.run_sync(canonical_metadata.drop_all)
    await engine.dispose()


async def test_occurrence_store_accepts_only_authenticated_direct_request(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, _engine = occurrence_runtime
    requester_result = await mailboxes.register_agent("Requester", "principal-a", "chat-a")
    reviewer_result = await mailboxes.register_agent("Reviewer", "principal-b", "chat-b")
    requester, reviewer = requester_result.mailbox, reviewer_result.mailbox
    assert requester is not None and reviewer is not None
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    revision = canonical_revision(subject.work_id, subject.row_version)
    review_id = uuid4()
    brief = CanonicalReviewBrief(
        basis=ReviewBasis(
            review_id=review_id, subject_work_id=subject_id, subject_revision=revision,
            review_kind="CODE", mode="FULL", requester_endpoint_id=requester.endpoint_id,
            requester_generation=requester.generation, policy_version="policy-v1",
            guidelines_version="guidelines-v1", guidelines_digest="a" * 64,
        ),
        subject_title=subject.title, material_claim=subject.notes,
        instructions=("Independently falsify the claim.",),
    )
    assert await occurrences.ensure(brief)
    assert await occurrences.ensure(brief)
    envelope = ReviewEnvelope(
        type="REVIEW_REQUEST", brief=brief, requester_name=requester.name,
        requester_endpoint_id=requester.endpoint_id,
    )
    route = MessageRoute(
        recipient_work_id=reviewer.endpoint_id,
        recipient_grant_version=reviewer.generation,
    )
    async def send(sender_id: UUID, message_id: UUID) -> None:
        result = await messages.submit_admitted(
            sender_id, route,
            MessageSubmitRequest(
                api_version="1", message_id=message_id,
                grant_version=requester.generation, route_ref="review.request",
                kind="request", payload=envelope.model_dump(mode="json"),
            ),
            agent_binding=requester,
        )
        assert result.status == "ok"

    await send(requester.endpoint_id, uuid4())
    await send(requester.endpoint_id, _stable(
        "reviewer-request", review_id, revision, reviewer.endpoint_id,
    ))
    deliveries = await occurrences.request_deliveries(review_id)
    assert len(deliveries) == 1
    assert deliveries[0][1] == envelope
    assert deliveries[0][0].sender_work_id == requester.endpoint_id


async def test_direct_request_is_server_briefed_idempotent_and_revision_bound(
    occurrence_runtime,
):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    requester_result = await mailboxes.register_agent("Requester", "principal-a", "chat-a")
    reviewer_result = await mailboxes.register_agent("Reviewer", "principal-b", "chat-b")
    requester, reviewer = requester_result.mailbox, reviewer_result.mailbox
    assert requester is not None and reviewer is not None
    service = ReviewService(
        occurrences, mailboxes, messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    revision = canonical_revision(subject.work_id, subject.row_version)
    request = ReviewRequest(
        subject_work_id=subject_id, observed_revision=revision,
        review_kind="CODE", candidate_ref="git:exact",
    )
    sent = await service.request(request, requester)
    assert sent.status == "SENT" and sent.delivery_id is not None
    assert await service.request(request, requester) == sent
    delivery, envelope = (await occurrences.request_deliveries(sent.review_id))[0]
    assert delivery.delivery_id == sent.delivery_id
    assert envelope.brief.material_claim == "Material claim"
    assert envelope.brief.named_evidence == ("git:exact",)

    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE canonical_work SET row_version = row_version + 1 WHERE work_id = :id"
        ), {"id": subject_id})
    stale = await service.request(request, requester)
    assert (stale.status, stale.reason) == ("STALE", "subject_revision_changed")
