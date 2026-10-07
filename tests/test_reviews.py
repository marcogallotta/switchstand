import asyncio
import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, insert, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import switchstand.state
from switchstand.agent_mailboxes import AgentMailboxState
from switchstand.canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_metadata,
    canonical_revision,
)
from switchstand.flow_report import report
from switchstand.grant_state import GrantState
from switchstand.messages import (
    MessageReceiveRequest,
    MessageRoute,
    MessageState,
    MessageSubmitRequest,
    RuntimeCurrentness,
)
from switchstand.messages import (
    messages as message_rows,
)
from switchstand.reviews import (
    CanonicalReviewBrief,
    ReviewBasis,
    ReviewEnvelope,
    ReviewerBound,
    ReviewFinding,
    ReviewGuidelines,
    ReviewOccurrenceState,
    ReviewPolicy,
    ReviewRequest,
    ReviewService,
    ReviewSubmit,
    _stable,
)
from switchstand.state import work_handles, work_migration_receipts


def _independent_services(
    occurrences: ReviewOccurrenceState, policy: ReviewPolicy,
) -> tuple[tuple[ReviewService, ...], tuple[AsyncEngine, ...], tuple[str, ...]]:
    services, engines, applications = [], [], []
    database_url = occurrences.engine.url.render_as_string(hide_password=False)
    for _ in range(2):
        application = f"typed-review-race-{uuid4()}"
        engine = create_async_engine(
            database_url, connect_args={"application_name": application},
        )
        mailboxes = AgentMailboxState(engine)
        occurrence_state = ReviewOccurrenceState(
            CanonicalWorkRepository(engine), mailboxes, policy,
        )
        services.append(ReviewService(
            occurrence_state, mailboxes, MessageState(engine, GrantState(engine)), policy,
            ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
        ))
        engines.append(engine)
        applications.append(application)
    return tuple(services), tuple(engines), tuple(applications)


async def _race_while_occurrence_locked(
    engine: AsyncEngine, review_id: UUID, calls, applications: tuple[str, str],
):
    tasks = []
    try:
        async with engine.begin() as blocker:
            locked = await blocker.scalar(text(
                "SELECT work_id FROM canonical_work "
                "WHERE work_id = :review_id FOR UPDATE"
            ), {"review_id": review_id})
            assert locked == review_id
            tasks = [asyncio.create_task(call()) for call in calls]
            deadline = asyncio.get_running_loop().time() + 2
            while True:
                async with engine.connect() as monitor:
                    waiting = await monitor.scalar(text(
                        "SELECT count(DISTINCT application_name) FROM pg_stat_activity "
                        "WHERE application_name IN (:first, :second) "
                        "AND wait_event_type = 'Lock'"
                    ), {"first": applications[0], "second": applications[1]})
                if waiting == 2:
                    break
                assert not any(task.done() for task in tasks)
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("both review calls did not reach the database lock")
                await asyncio.sleep(0.01)
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


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


async def test_same_principal_distinct_endpoints_complete_review(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, _engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-shared", "chat-requester",
    )).mailbox
    reviewer = (await mailboxes.register_agent(
        "Reviewer", "principal-shared", "chat-reviewer",
    )).mailbox
    assert requester is not None and reviewer is not None
    assert requester.endpoint_id != reviewer.endpoint_id
    service = ReviewService(
        occurrences, mailboxes, messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    request = ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(subject.work_id, subject.row_version),
        review_kind="CODE",
    )
    sent = await service.request(request, requester)
    assert sent.status == "SENT" and sent.review_id is not None
    assert sent.delivery_id is not None
    assert await service.request(request, requester) == sent
    assert len(await occurrences.request_sources(sent.review_id)) == 1

    received = await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(
            generation=str(reviewer.generation),
            current_generation=str(reviewer.generation),
        ),
        MessageReceiveRequest(
            api_version="1", delivery_id=sent.delivery_id,
            grant_version=reviewer.generation,
        ),
        agent_binding=reviewer,
    )
    assert received.status == "ok"
    submitted = await service.submit(ReviewSubmit(
        review_id=sent.review_id, verdict="PASS", context_provenance="UNSEEDED",
    ), reviewer)
    assert submitted.status == "SUBMITTED"
    assert (await service.get(sent.review_id, requester)).status == "PASS"


async def test_same_endpoint_reviewer_denial_leaves_no_review_state(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, _engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-shared", "chat-requester",
    )).mailbox
    assert requester is not None
    self_review_policy = ReviewPolicy(
        version="policy-v1", reviewer_by_kind={"CODE": "Requester"},
    )
    service = ReviewService(
        occurrences, mailboxes, messages, self_review_policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    request = ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(subject.work_id, subject.row_version),
        review_kind="CODE",
    )
    basis = service._basis(request, requester)
    denied = await service.request(request, requester)

    assert (denied.status, denied.reason, denied.review_id) == (
        "DENIED", "reviewer_not_eligible", basis.review_id,
    )
    assert await occurrences.works.get(basis.review_id) is None
    assert await occurrences.basis_sources(basis.review_id) == ()
    assert await occurrences.stored_request_sources(basis.review_id) == ()


async def test_sent_request_replay_survives_reviewer_principal_transfer(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, _engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-requester",
    )).mailbox
    reviewer = (await mailboxes.register_agent(
        "Reviewer", "principal-b", "chat-reviewer",
    )).mailbox
    assert requester is not None and reviewer is not None
    service = ReviewService(
        occurrences, mailboxes, messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    request = ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(subject.work_id, subject.row_version),
        review_kind="CODE",
    )
    sent = await service.request(request, requester)
    assert sent.status == "SENT" and sent.delivery_id is not None
    transfer = await mailboxes.request_transfer(
        "Reviewer", "principal-a", "chat-reviewer-shared",
    )
    assert transfer.status == "pending" and transfer.request_id is not None
    approved = await mailboxes.approve_transfer(transfer.request_id)
    assert approved.status == "approved"

    replay = await service.request(request, requester)

    assert replay == sent
    assert len(await occurrences.stored_request_sources(sent.review_id)) == 1


async def test_concurrent_review_request_converges_on_one_occurrence_and_delivery(
    occurrence_runtime,
):
    occurrences, _messages, mailboxes, subject_id, engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a",
    )).mailbox
    reviewer = (await mailboxes.register_agent(
        "Reviewer", "principal-b", "chat-b",
    )).mailbox
    assert requester is not None and reviewer is not None
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    request = ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(subject.work_id, subject.row_version),
        review_kind="CODE",
    )
    services, engines, applications = _independent_services(
        occurrences, occurrences.policy,
    )
    basis = services[0]._basis(request, requester)
    assert await occurrences.ensure(services[0]._brief(basis, subject))
    try:
        results = await _race_while_occurrence_locked(
            engine, basis.review_id,
            tuple(lambda service=service: service.request(
                request, requester,
            ) for service in services),
            applications,
        )
    finally:
        await asyncio.gather(*(current.dispose() for current in engines))

    assert {result.status for result in results} == {"SENT"}
    assert len({result.review_id for result in results}) == 1
    assert len({result.delivery_id for result in results}) == 1
    review_id = results[0].review_id
    assert review_id is not None
    assert len(await occurrences.basis_sources(review_id)) == 1
    assert len(await occurrences.request_sources(review_id)) == 1


async def test_concurrent_review_get_converges_on_one_reviewer_delivery(
    occurrence_runtime,
):
    occurrences, _messages, mailboxes, subject_id, engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a",
    )).mailbox
    assert requester is not None
    no_reviewer = ReviewPolicy(version="policy-v1", reviewer_by_kind={})
    waiting_service = ReviewService(
        occurrences, mailboxes, _messages, no_reviewer,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    waiting = await waiting_service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(subject.work_id, subject.row_version),
        review_kind="CODE",
    ), requester)
    assert waiting.status == "WAITING_REVIEWER" and waiting.review_id is not None
    reviewer = (await mailboxes.register_agent(
        "Reviewer", "principal-b", "chat-b",
    )).mailbox
    assert reviewer is not None
    policy = ReviewPolicy(version="policy-v1", reviewer_by_kind={"CODE": "Reviewer"})
    occurrences.policy = policy
    services, engines, applications = _independent_services(occurrences, policy)
    try:
        results = await _race_while_occurrence_locked(
            engine, waiting.review_id,
            tuple(lambda service=service: service.get(
                waiting.review_id, requester,
            ) for service in services),
            applications,
        )
    finally:
        await asyncio.gather(*(current.dispose() for current in engines))

    assert {result.status for result in results} == {"REQUEST_UNPICKED"}
    assert len({result.delivery_id for result in results}) == 1
    sources = await occurrences.request_sources(waiting.review_id)
    assert len(sources) == 1
    assert sources[0][0].delivery.recipient_work_id == reviewer.endpoint_id
    assert sources[0][2] is None


async def test_concurrent_review_recover_converges_on_one_generation_transition(
    occurrence_runtime,
):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a",
    )).mailbox
    reviewer = (await mailboxes.register_agent(
        "Reviewer", "principal-b", "chat-b",
    )).mailbox
    assert requester is not None and reviewer is not None
    service = ReviewService(
        occurrences, mailboxes, messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    sent = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(subject.work_id, subject.row_version),
        review_kind="CODE",
    ), requester)
    assert sent.review_id is not None and sent.delivery_id is not None
    received = await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(generation="1", current_generation="1"),
        MessageReceiveRequest(
            api_version="1", delivery_id=sent.delivery_id, grant_version=1,
        ),
        agent_binding=reviewer,
    )
    assert received.status == "ok"
    replacement = (await mailboxes.takeover(
        "Reviewer", "principal-b", "chat-b-replacement",
    )).mailbox
    assert replacement is not None and replacement.generation == 2
    services, engines, applications = _independent_services(
        occurrences, occurrences.policy,
    )
    transitions = []
    for current in engines:
        event.listen(
            current.sync_engine, "after_cursor_execute",
            lambda _connection, _cursor, statement, _parameters, _context, _many: (
                transitions.append(statement)
                if statement.lstrip().startswith("UPDATE message_deliveries") else None
            ),
        )
    try:
        results = await _race_while_occurrence_locked(
            engine, sent.review_id,
            tuple(lambda current=service: current.recover(
                sent.review_id, replacement,
            ) for service in services),
            applications,
        )
    finally:
        await asyncio.gather(*(current.dispose() for current in engines))

    assert {result.status for result in results} == {"RECEIVED"}
    assert {result.delivery_id for result in results} == {sent.delivery_id}
    assert len(transitions) == 1
    async with engine.connect() as connection:
        generation = (await connection.execute(text(
            "SELECT recipient_grant_version, receiving_generation "
            "FROM message_deliveries WHERE delivery_id = :id"
        ), {"id": sent.delivery_id})).one()
    assert tuple(generation) == (2, "2")


async def test_received_review_submits_authoritative_pass(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a"
    )).mailbox
    reviewer = (await mailboxes.register_agent(
        "Reviewer", "principal-b", "chat-b"
    )).mailbox
    assert requester is not None and reviewer is not None
    service = ReviewService(
        occurrences, mailboxes, messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    revision = canonical_revision(subject.work_id, subject.row_version)
    async with engine.begin() as connection:
        await connection.execute(insert(work_migration_receipts).values(
            name="typed-review-v1-cutoff", source_digest="b" * 64,
        ))
    sent = await service.request(ReviewRequest(
        subject_work_id=subject_id, observed_revision=revision, review_kind="CODE",
    ), requester)
    assert sent.review_id is not None and sent.delivery_id is not None
    assert (await service.get(sent.review_id, requester)).status == "REQUEST_UNPICKED"
    received = await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(
            generation=str(reviewer.generation),
            current_generation=str(reviewer.generation),
        ),
        MessageReceiveRequest(
            api_version="1", delivery_id=sent.delivery_id,
            grant_version=reviewer.generation,
        ),
        agent_binding=reviewer,
    )
    assert received.status == "ok"
    reviewer = (await mailboxes.takeover(
        "Reviewer", "principal-b", "chat-b-replacement",
    )).mailbox
    assert reviewer is not None and reviewer.generation == 2
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE message_deliveries SET recipient_grant_version = 2 "
            "WHERE delivery_id = :id"
        ), {"id": sent.delivery_id})
    inconsistent = await service.recover(sent.review_id, reviewer)
    assert (inconsistent.status, inconsistent.reason) == ("DENIED", "recovery_not_allowed")
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE message_deliveries SET recipient_grant_version = 1 "
            "WHERE delivery_id = :id"
        ), {"id": sent.delivery_id})
    recovered = await service.recover(sent.review_id, reviewer)
    assert recovered.status == "RECEIVED" and recovered.delivery_id == sent.delivery_id, recovered
    assert await service.recover(sent.review_id, reviewer) == recovered
    submitted = await service.submit(ReviewSubmit(
        review_id=sent.review_id, verdict="PASS", context_provenance="UNSEEDED",
    ), reviewer)
    assert submitted.status == "SUBMITTED"
    async with engine.begin() as connection:
        assert await occurrences.pass_status_in_transaction(
            connection, subject_id, revision,
        ) == "PASS"
        await connection.execute(text(
            "UPDATE canonical_work SET notes = 'advanced', row_version = row_version + 1 "
            "WHERE work_id = :id"
        ), {"id": subject_id})
    stale = await service.submit(ReviewSubmit(
        review_id=sent.review_id, verdict="PASS", context_provenance="UNSEEDED",
    ), reviewer)
    assert (stale.status, stale.reason) == ("STALE", "subject_revision_changed")

    current = await occurrences.works.get(subject_id)
    assert current is not None
    current_revision = canonical_revision(current.work_id, current.row_version)
    basis = ReviewBasis(
        review_id=sent.review_id, subject_work_id=subject_id,
        subject_revision=current_revision, review_kind="CODE", mode="FULL",
        requester_endpoint_id=requester.endpoint_id,
        requester_generation=requester.generation, policy_version="policy-v1",
        guidelines_version="guidelines-v1", guidelines_digest="a" * 64,
    )
    brief = CanonicalReviewBrief(
        basis=basis, subject_title=current.title, material_claim=current.notes,
        instructions=("Adversarial current request sharing historical identity.",),
    )
    route = MessageRoute(
        recipient_work_id=reviewer.endpoint_id,
        recipient_grant_version=reviewer.generation,
    )
    forged_current = await messages.submit_admitted(
        requester.endpoint_id, route,
        MessageSubmitRequest(
            api_version="1",
            message_id=_stable(
                "reviewer-request", sent.review_id, current_revision, reviewer.endpoint_id,
            ),
            grant_version=requester.generation, route_ref="review.request", kind="request",
            payload=ReviewEnvelope(
                type="REVIEW_REQUEST", brief=brief, requester_name=requester.name,
                requester_endpoint_id=requester.endpoint_id,
            ).model_dump(mode="json"),
        ),
        agent_binding=requester,
    )
    assert (forged_current.status, forged_current.reason) == ("denied", "reserved_route")
    mixed = await service.submit(ReviewSubmit(
        review_id=sent.review_id, verdict="PASS", context_provenance="UNSEEDED",
    ), reviewer)
    assert (mixed.status, mixed.reason) == ("STALE", "subject_revision_changed")
    async with engine.begin() as connection:
        assert await occurrences.pass_status_in_transaction(
            connection, subject_id, current_revision,
        ) == "NOT_PASS"


async def test_recovery_rejects_concluded_historical_delivery(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a"
    )).mailbox
    reviewer = (await mailboxes.register_agent(
        "Reviewer", "principal-b", "chat-b"
    )).mailbox
    assert requester is not None and reviewer is not None
    service = ReviewService(
        occurrences, mailboxes, messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    sent = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(subject.work_id, subject.row_version),
        review_kind="CODE",
    ), requester)
    assert sent.review_id is not None and sent.delivery_id is not None
    received = await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(generation="1", current_generation="1"),
        MessageReceiveRequest(
            api_version="1", delivery_id=sent.delivery_id, grant_version=1,
        ),
        agent_binding=reviewer,
    )
    assert received.status == "ok"
    submitted = await service.submit(ReviewSubmit(
        review_id=sent.review_id, verdict="PASS", context_provenance="UNSEEDED",
    ), reviewer)
    assert submitted.status == "SUBMITTED"
    replacement = (await mailboxes.takeover(
        "Reviewer", "principal-b", "chat-b-replacement",
    )).mailbox
    assert replacement is not None and replacement.generation == 2
    denied = await service.recover(sent.review_id, replacement)
    assert (denied.status, denied.reason) == ("DENIED", "recovery_not_allowed")
    async with engine.connect() as connection:
        generation = (await connection.execute(text(
            "SELECT recipient_grant_version, receiving_generation "
            "FROM message_deliveries WHERE delivery_id = :id"
        ), {"id": sent.delivery_id})).one()
    assert tuple(generation) == (1, "1")

async def test_flow_report_projects_exact_current_review_pickup(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a"
    )).mailbox
    reviewer = (await mailboxes.register_agent(
        "Reviewer", "principal-b", "chat-b"
    )).mailbox
    assert requester is not None and reviewer is not None
    service = ReviewService(
        occurrences, mailboxes, messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    revision = canonical_revision(subject.work_id, subject.row_version)

    missing = (await report(engine, subject_id, occurrences))["review_pickup"]
    assert missing == {
        "status": "UNKNOWN", "reason": "NO_CURRENT_OCCURRENCE", "phase": None,
        "unpicked": None,
        "oldest_request_age_ms": None, "requested_at": None,
        "received_at": None, "verdict_at": None,
    }
    sent = await service.request(ReviewRequest(
        subject_work_id=subject_id, observed_revision=revision, review_kind="CODE",
    ), requester)
    assert sent.review_id is not None and sent.delivery_id is not None
    waiting = (await report(engine, subject_id, occurrences))["review_pickup"]
    assert waiting["status"] == "KNOWN" and waiting["unpicked"] is True
    assert waiting["phase"] == "REQUEST_UNPICKED"
    assert isinstance(waiting["oldest_request_age_ms"], int)
    assert waiting["requested_at"] is not None
    assert waiting["received_at"] is None and waiting["verdict_at"] is None
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE message_deliveries SET received_at = now() + interval '1 day' "
            "WHERE delivery_id = :id"
        ), {"id": sent.delivery_id})
    unsafe = (await report(engine, subject_id, occurrences))["review_pickup"]
    assert (unsafe["status"], unsafe["reason"]) == ("UNKNOWN", "INCONSISTENT_TIMESTAMPS")
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE message_deliveries SET received_at = NULL WHERE delivery_id = :id"
        ), {"id": sent.delivery_id})

    received = await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(
            generation=str(reviewer.generation),
            current_generation=str(reviewer.generation),
        ),
        MessageReceiveRequest(
            api_version="1", delivery_id=sent.delivery_id,
            grant_version=reviewer.generation,
        ),
        agent_binding=reviewer,
    )
    assert received.status == "ok"
    picked = (await report(engine, subject_id, occurrences))["review_pickup"]
    assert picked["status"] == "KNOWN" and picked["unpicked"] is False
    assert picked["phase"] == "RECEIVED"
    assert datetime.fromisoformat(picked["received_at"]).tzinfo is not None
    submitted = await service.submit(ReviewSubmit(
        review_id=sent.review_id, verdict="PASS", context_provenance="UNSEEDED",
    ), reviewer)
    assert submitted.status == "SUBMITTED"
    decided = (await report(engine, subject_id, occurrences))["review_pickup"]
    assert decided["status"] == "KNOWN" and decided["unpicked"] is False
    assert decided["phase"] == "VERDICT"
    assert datetime.fromisoformat(decided["verdict_at"]).astimezone(UTC) <= datetime.now(UTC)

    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE canonical_work SET row_version = row_version + 1 WHERE work_id = :id"
        ), {"id": subject_id})
    obsolete = (await report(engine, subject_id, occurrences))["review_pickup"]
    assert obsolete["status"] == "UNKNOWN"
    assert obsolete["reason"] == "NO_CURRENT_OCCURRENCE"


async def test_review_pickup_rejects_multiple_current_occurrences(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    reviewer = (await mailboxes.register_agent(
        "Reviewer", "principal-reviewer", "chat-reviewer"
    )).mailbox
    assert reviewer is not None
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    revision = canonical_revision(subject.work_id, subject.row_version)
    service = ReviewService(
        occurrences, mailboxes, messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    for suffix in ("a", "b"):
        requester = (await mailboxes.register_agent(
            f"Requester {suffix}", f"principal-{suffix}", f"chat-{suffix}"
        )).mailbox
        assert requester is not None
        sent = await service.request(ReviewRequest(
            subject_work_id=subject_id, observed_revision=revision, review_kind="CODE",
        ), requester)
        assert sent.status == "SENT"

    result = (await report(engine, subject_id, occurrences))["review_pickup"]
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "MULTIPLE_CURRENT_OCCURRENCES"


async def test_owner_polling_acquires_independent_reviewer(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a"
    )).mailbox
    assert requester is not None
    occurrences.policy = ReviewPolicy(version="policy-v1", reviewer_by_kind={})
    service = ReviewService(
        occurrences, mailboxes, messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    waiting = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(subject.work_id, subject.row_version),
        review_kind="CODE",
    ), requester)
    assert waiting.review_id is not None and waiting.delivery_id is None
    reviewer = (await mailboxes.register_agent(
        "Reviewer", "principal-b", "chat-b"
    )).mailbox
    assert reviewer is not None
    service.policy = ReviewPolicy(
        version="policy-v1", reviewer_by_kind={"CODE": "Reviewer"},
    )
    sent = await service.get(waiting.review_id, requester)
    assert sent.status == "REQUEST_UNPICKED" and sent.delivery_id is not None, sent
    requested = (await report(engine, subject_id, occurrences))["review_pickup"]
    assert requested["phase"] == "REQUEST_UNPICKED"
    assert requested["unpicked"] is True
    sources = await occurrences.request_sources(waiting.review_id)
    assert len(sources) == 1
    record, _envelope, bound = sources[0]
    assert record.delivery.sender_work_id == requester.endpoint_id
    assert bound is None
    received = await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(
            generation=str(reviewer.generation),
            current_generation=str(reviewer.generation),
        ),
        MessageReceiveRequest(
            api_version="1", delivery_id=sent.delivery_id,
            grant_version=reviewer.generation,
        ),
        agent_binding=reviewer,
    )
    assert received.status == "ok"
    assert (await service.get(waiting.review_id, requester)).status == "RECEIVED"
    submitted = await service.submit(ReviewSubmit(
        review_id=waiting.review_id, verdict="PASS", context_provenance="UNSEEDED",
    ), reviewer)
    assert submitted.status == "SUBMITTED"
    assert (await service.get(waiting.review_id, requester)).status == "PASS"
    async with engine.begin() as connection:
        subject = await occurrences.works.get(subject_id)
        assert subject is not None
        assert await occurrences.pass_status_in_transaction(
            connection, subject_id,
            canonical_revision(subject.work_id, subject.row_version),
        ) == "PASS"


async def test_focused_rereview_requires_authoritative_named_finding(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a"
    )).mailbox
    reviewer = (await mailboxes.register_agent(
        "Reviewer", "principal-b", "chat-b"
    )).mailbox
    assert requester is not None and reviewer is not None
    service = ReviewService(
        occurrences, mailboxes, messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    initial_request_revision = canonical_revision(subject.work_id, subject.row_version)
    arbitrary = await service.request(ReviewRequest(
        subject_work_id=subject_id, observed_revision=initial_request_revision,
        review_kind="CODE", prior_review_id=uuid4(),
    ), requester)
    assert (arbitrary.status, arbitrary.reason) == ("DENIED", "prior_review_not_found")
    initial = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=initial_request_revision,
        review_kind="CODE",
    ), requester)
    assert initial.review_id is not None and initial.delivery_id is not None
    received = await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(
            generation=str(reviewer.generation), current_generation=str(reviewer.generation),
        ),
        MessageReceiveRequest(
            api_version="1", delivery_id=initial.delivery_id,
            grant_version=reviewer.generation,
        ),
        agent_binding=reviewer,
    )
    assert received.status == "ok"
    finding = ReviewFinding(
        finding_id="F-1", defect="Missing proof", evidence="No boundary result",
        consequence="Claim is unsafe", affected_claim="landing",
        minimum_clearing_condition="Supply boundary proof",
    )
    submitted = await service.submit(ReviewSubmit(
        review_id=initial.review_id, verdict="FINDINGS", findings=(finding,),
        context_provenance="UNSEEDED",
    ), reviewer)
    assert submitted.status == "SUBMITTED"
    same_revision = await service.request(ReviewRequest(
        subject_work_id=subject_id, observed_revision=initial_request_revision,
        review_kind="CODE", mode="FOCUSED", prior_review_id=initial.review_id,
        finding_ids=("F-1",),
    ), requester)
    assert (same_revision.status, same_revision.reason) == ("DENIED", "prior_review_not_found")
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE canonical_work SET notes = 'corrected', row_version = row_version + 1 "
            "WHERE work_id = :id"
        ), {"id": subject_id})
    corrected = await occurrences.works.get(subject_id)
    assert corrected is not None
    focused = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(corrected.work_id, corrected.row_version),
        review_kind="CODE", mode="FOCUSED", prior_review_id=initial.review_id,
        finding_ids=("F-1",),
    ), requester)
    assert focused.status == "SENT"
    delivery = await occurrences.request_delivery(
        initial.review_id,
        subject_revision=canonical_revision(corrected.work_id, corrected.row_version),
    )
    assert delivery is not None
    assert "secondary/global-impact" in delivery[1].brief.instructions[-1]
    transition = delivery[1].brief.focused_from
    assert transition is not None
    assert transition.prior_basis.subject_revision == initial_request_revision
    assert transition.prior_material_claim == "Material claim"
    assert transition.prior_named_evidence == ()
    assert transition.changed_basis_fields == ("subject_revision",)
    assert delivery[1].brief.material_claim == "corrected"

    upgraded = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(corrected.work_id, corrected.row_version),
        review_kind="CODE", prior_review_id=initial.review_id,
    ), requester)
    assert (upgraded.status, upgraded.reason) == ("UNKNOWN", "duplicate_authoritative_state")
    assert (await service.recover(initial.review_id, reviewer)).reason == "duplicate_authoritative_state"


async def test_acquisition_is_atomic_and_bundle_is_fenced(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    coordinator = (await mailboxes.register_agent(
        "Coordinator", "principal-c", "chat-c",
    )).mailbox
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a",
    )).mailbox
    assert coordinator is not None and requester is not None
    service = ReviewService(
        occurrences, mailboxes, messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    waiting = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(subject_id, subject.row_version),
        review_kind="CODE",
        candidate_ref="github:marcogallotta/switchstand:pr/7",
    ), requester)
    assert waiting.status == "WAITING_REVIEWER" and waiting.review_id is not None
    basis_source = await occurrences.basis_sources(waiting.review_id)
    assert len(basis_source) == 1
    envelope = basis_source[0][1]
    submitted = await messages.submit_admitted(
        requester.endpoint_id,
        MessageRoute(
            recipient_work_id=coordinator.endpoint_id,
            recipient_grant_version=coordinator.generation,
        ),
        MessageSubmitRequest(
            api_version="1",
            message_id=_stable(
                "acquisition", waiting.review_id, envelope.brief.basis.subject_revision,
            ),
            grant_version=requester.generation,
            route_ref="review.acquisition",
            kind="request",
            payload=envelope.model_dump(mode="json"),
        ),
        agent_binding=requester,
    )
    assert submitted.status == "ok" and submitted.message is not None
    received = await messages.receive_admitted(
        coordinator.endpoint_id, coordinator.generation,
        RuntimeCurrentness(generation="1", current_generation="1"),
        MessageReceiveRequest(
            api_version="1", delivery_id=submitted.message.delivery_id,
            grant_version=coordinator.generation,
        ),
        agent_binding=coordinator,
    )
    assert received.status == "ok"
    reviewer = (await mailboxes.register_agent(
        "Reviewer", "principal-b", "chat-b",
    )).mailbox
    assert reviewer is not None
    sent = await service.continue_acquisition(waiting.review_id, coordinator)
    assert sent.status == "SENT" and sent.delivery_id is not None
    assert len(await occurrences.stored_request_sources(waiting.review_id)) == 1
    assert len(await occurrences.request_sources(waiting.review_id)) == 1
    assert (await service.bundle_access(waiting.review_id, reviewer)).status == "DENIED"
    picked_up = await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(generation="1", current_generation="1"),
        MessageReceiveRequest(
            api_version="1", delivery_id=sent.delivery_id,
            grant_version=reviewer.generation,
        ),
        agent_binding=reviewer,
    )
    assert picked_up.status == "ok"
    access = await service.bundle_access(waiting.review_id, reviewer)
    assert access.status == "AUTHORIZED" and access.basis is not None
    assert (await service.bundle_access(
        waiting.review_id, reviewer, access.basis,
    )).status == "READY"
    service.policy = ReviewPolicy(version="policy-v1", reviewer_by_kind={
        "CODE": "Reviewer", "DESIGN": "Reviewer",
    })
    occurrences.policy = service.policy
    unsupported = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(subject_id, subject.row_version),
        review_kind="DESIGN", candidate_ref="github:marcogallotta/switchstand:pr/8",
    ), requester)
    assert unsupported.delivery_id is not None and unsupported.review_id is not None
    picked_up = await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(generation="1", current_generation="1"),
        MessageReceiveRequest(api_version="1", delivery_id=unsupported.delivery_id,
                              grant_version=reviewer.generation),
        agent_binding=reviewer,
    )
    assert picked_up.status == "ok"
    unsupported_access = await service.bundle_access(unsupported.review_id, reviewer)
    assert (unsupported_access.status, unsupported_access.reason) == (
        "UNKNOWN", "REVIEW_KIND_UNSUPPORTED_V1",
    )
    async with engine.begin() as connection:
        acknowledgement = ReviewerBound(review_id=waiting.review_id,
            reviewer_delivery_id=sent.delivery_id, reviewer_name=reviewer.name)
        target = (message_rows.c.route_ref == "review.acquisition") & (
            message_rows.c.kind == "result")
        await connection.execute(update(message_rows).where(target).values(payload={}))
    assert (await service.bundle_access(
        waiting.review_id, reviewer, access.basis,
    )).status == "UNKNOWN"
    async with engine.begin() as connection:
        await connection.execute(update(message_rows).where(target).values(
            payload=acknowledgement.model_dump(mode="json")))
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE canonical_work SET row_version = row_version + 1 WHERE work_id = :id"
        ), {"id": subject_id})
    assert (await service.bundle_access(
        waiting.review_id, reviewer, access.basis,
    )).status == "STALE"
