import asyncio
import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import switchstand.state
from switchstand.agent_mailboxes import AgentMailboxState
from switchstand.canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_metadata,
    canonical_revision,
)
from switchstand.contracts import LaunchAuthority
from switchstand.flow_report import report
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext, WorkGrant
from switchstand.human_reviews import (
    HumanReviewBaselineV2,
    HumanReviewConsequence,
    HumanReviewDecisionContextV2,
    HumanReviewOpeningV2,
    HumanReviewState,
)
from switchstand.implementation_requests import ImplementationRequestState
from switchstand.messages import (
    MessageReceiveRequest,
    MessageRoute,
    MessageState,
    MessageSubmitRequest,
    RuntimeCurrentness,
)
from switchstand.reviews import (
    BaselineAssessment,
    BaselineCheck,
    CanonicalReviewBrief,
    ReviewBasis,
    ReviewEnvelope,
    ReviewFinding,
    ReviewGuidelines,
    ReviewOccurrenceState,
    ReviewPolicy,
    ReviewRequest,
    ReviewResult,
    ReviewService,
    ReviewSubmit,
    _stable,
)
from switchstand.state import work_handles, work_migration_receipts


def test_review_result_guidance_preserves_role_and_authority_boundaries():
    review_id, delivery_id = uuid4(), uuid4()
    submitted = ReviewResult(
        status="SUBMITTED", review_id=review_id, delivery_id=delivery_id,
    )
    assert "requester-only review_get" in submitted.next_action
    assert "Poll review_get" not in submitted.next_action

    finding = ReviewFinding(
        finding_id="F1", defect="Defect", evidence="Evidence",
        consequence="Consequence", affected_claim="Claim",
        minimum_clearing_condition="Clear it",
    )
    findings = ReviewResult(
        status="FINDINGS", review_id=review_id, delivery_id=delivery_id,
        verdict="FINDINGS", findings=(finding,), verdict_digest="a" * 64,
    )
    for required in (
        "against evidence", "accepted defects only", "mode=FOCUSED",
        "prior_review_id=<this review_id>", "finding_ids=<accepted finding_ids>",
        "observed_revision=<new revision>", "scope expansion",
    ):
        assert required in findings.next_action

    passed = ReviewResult(
        status="PASS", review_id=review_id, delivery_id=delivery_id,
        verdict="PASS", verdict_digest="b" * 64,
    )
    for required in (
        "evidence, not scope or effect authority", "runnable owned work",
        "live docs/human-review.md decision conversation",
    ):
        assert required in passed.next_action


def test_review_result_guidance_distinguishes_unavailable_reviewer_from_active_polling():
    review_id, delivery_id = uuid4(), uuid4()
    waiting = ReviewResult(
        status="WAITING_REVIEWER", review_id=review_id,
        reason="reviewer_not_available",
    )
    assert "No eligible reviewer" in waiting.next_action
    assert "no reviewer delivery exists" in waiting.next_action
    assert "distinct from REQUEST_UNPICKED" in waiting.next_action
    assert "exact review_id and bounded watch" in waiting.next_action
    assert "duplicate request" in waiting.next_action

    for status in ("SENT", "REQUEST_UNPICKED", "RECEIVED"):
        result = ReviewResult(
            status=status, review_id=review_id, delivery_id=delivery_id,
        )
        for required in (
            "while this chat can run", "bounded active polling", "Bash sleep 20",
            "review_get for this exact review_id", "empty read is nonterminal",
            "No verdict yet", "Do not end the turn", "until a terminal verdict",
            "Continue other runnable assigned work", "Sleep before the first poll",
            "every pending response requires continued work or another sleep and poll",
            "real host/session limit", "preserve the exact review_id and state",
            "do not use an hourly Scheduled task",
        ):
            assert required in result.next_action
        assert result.next_action != waiting.next_action


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


def _v2_consequence(subject_id: UUID, revision: str) -> HumanReviewConsequence:
    return HumanReviewConsequence(
        package_work_id=subject_id, package_revision=revision,
        implementation_scope=("Implement reviewed package",),
        implementation_target="repository main", excluded_effects=("activation",),
        approval_effect="CLEAR_HUMAN_REVIEW_FOR_NAMED_CONTINUATION",
        baseline=HumanReviewBaselineV2(
            human_source_refs=("human:approved",),
            opening=HumanReviewOpeningV2(
                what_you_asked="Ship it", what_this_does="Implements it",
                what_this_does_not_or_changes_for_you="Does not activate", size="Small",
            ), approved_meaning=("Ship reviewed behavior",),
            protected_requirements=("Preserve behavior",), approved_non_goals=("Activation",),
            decision_context=HumanReviewDecisionContextV2(
                size_or_burden=("Small",), scale_assumptions=(),
                material_risks_and_boundaries=("No activation",),
            ), continuation_effects=("IMPLEMENT", "LAND"),
        ),
    )


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
    await works.create(CurrentWork(
        subject_id, "Exact package", False, "Material claim", lifecycle_state="CURRENT",
    ))
    mailboxes = AgentMailboxState(engine)
    policy = ReviewPolicy(version="policy-v1")
    yield ReviewOccurrenceState(works, mailboxes, policy), MessageState(
        engine, GrantState(engine)
    ), mailboxes, subject_id, engine
    async with engine.begin() as connection:
        await connection.execute(text(
            "TRUNCATE TABLE work_handles, agent_mailboxes, messages RESTART IDENTITY CASCADE"
        ))
        await connection.run_sync(canonical_metadata.drop_all)
    await engine.dispose()


async def register_coordinator(
    mailboxes: AgentMailboxState, principal: str, session: str,
):
    return await mailboxes.register_agent("Coordinator", principal, session)


async def test_occurrence_store_accepts_only_authenticated_direct_request(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, _engine = occurrence_runtime
    requester_result = await mailboxes.register_agent("Requester", "principal-a", "chat-a")
    reviewer_result = await register_coordinator(mailboxes, "principal-b", "chat-b")
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
    reviewer_result = await register_coordinator(mailboxes, "principal-b", "chat-b")
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
    reviewer = (await register_coordinator(
        mailboxes, "principal-shared", "chat-reviewer",
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
    requester = (await register_coordinator(
        mailboxes, "principal-shared", "chat-requester",
    )).mailbox
    assert requester is not None
    self_review_policy = ReviewPolicy(version="policy-v1")
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


async def test_baseline_review_round_trip_binds_assessment_and_projects_proof(
    occurrence_runtime,
):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a",
    )).mailbox
    reviewer = (await register_coordinator(
        mailboxes, "principal-b", "chat-b",
    )).mailbox
    assert requester is not None and reviewer is not None
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    revision = canonical_revision(subject.work_id, subject.row_version)
    consequence = _v2_consequence(subject_id, revision)
    human_reviews = HumanReviewState(engine, occurrences.works)
    assert (await human_reviews.propose(consequence)).status == "PREPARED"
    assert (await human_reviews.submit(
        consequence.consequence_id, subject_id, revision, "APPROVED",
    )).status == "RECORDED"
    service = ReviewService(
        occurrences, mailboxes, messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    check = BaselineCheck(
        baseline_id=consequence.consequence_id, phase="PRE_IMPLEMENTATION",
        owner_disposition="BASELINE_PRESERVED", comparison="Exact plan preserves baseline",
        evidence_refs=("git:plan",),
    )
    sent = await service.request(ReviewRequest(
        subject_work_id=subject_id, observed_revision=revision, review_kind="CODE",
        baseline_check=check,
    ), requester)
    assert sent.status == "SENT" and sent.review_id and sent.delivery_id
    assert (await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(generation="1", current_generation="1"),
        MessageReceiveRequest(api_version="1", delivery_id=sent.delivery_id, grant_version=1),
        agent_binding=reviewer,
    )).status == "ok"
    missing = await service.submit(ReviewSubmit(
        review_id=sent.review_id, verdict="PASS", context_provenance="UNSEEDED",
    ), reviewer)
    assert (missing.status, missing.reason) == ("DENIED", "baseline_assessment_invalid")
    assessment = BaselineAssessment(
        baseline_id=check.baseline_id, phase=check.phase,
        disposition="BASELINE_PRESERVED", evidence_refs=("git:plan",),
    )
    assert not service._assessment_matches(
        check.model_copy(update={"owner_disposition": "UNKNOWN"}),
        ReviewSubmit(
            review_id=sent.review_id, verdict="PASS", context_provenance="UNSEEDED",
            baseline_assessment=assessment,
        ),
    )
    assert (await service.submit(ReviewSubmit(
        review_id=sent.review_id, verdict="PASS", context_provenance="UNSEEDED",
        baseline_assessment=assessment,
    ), reviewer)).status == "SUBMITTED"
    result = await service.get(sent.review_id, requester)
    assert result.status == "PASS" and result.baseline_assessment == assessment
    async with engine.begin() as connection:
        proof = await occurrences.baseline_pass_status_in_transaction(
            connection, subject_id, revision,
        )
    assert proof.status == "PASS" and proof.baseline_id == check.baseline_id
    principal = PrincipalContext(
        issuer="fixture", subject=f"implementation-{uuid4()}", client_id="codex", assurance="test",
    )
    grant = WorkGrant(
        id=uuid4(), version=1, principal=principal,
        authority=LaunchAuthority(active_work_id=subject_id),
        operations=frozenset({"implementation_request"}), issuer="fixture",
        provenance="direct assignment fixture", expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await GrantState(engine).issue(grant, None)
    admitted = await ImplementationRequestState(
        engine, occurrences.works, occurrences,
    ).request(principal, uuid4(), subject_id, revision)
    assert (admitted.status, admitted.reason) == ("APPLIED", None)


async def test_sent_request_replay_survives_reviewer_principal_transfer(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, _engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-requester",
    )).mailbox
    reviewer = (await register_coordinator(
        mailboxes, "principal-b", "chat-reviewer",
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
        "Coordinator", "principal-a", "chat-reviewer-shared",
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
    reviewer = (await register_coordinator(
        mailboxes, "principal-b", "chat-b",
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
    no_reviewer = ReviewPolicy(version="policy-v1")
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
    reviewer = (await register_coordinator(
        mailboxes, "principal-b", "chat-b",
    )).mailbox
    assert reviewer is not None
    policy = ReviewPolicy(version="policy-v1")
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
    reviewer = (await register_coordinator(
        mailboxes, "principal-b", "chat-b",
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
        "Coordinator", "principal-b", "chat-b-replacement",
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
    reviewer = (await register_coordinator(
        mailboxes, "principal-b", "chat-b"
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
    for _ in range(3):
        pending = await service.get(sent.review_id, requester)
        assert pending.status == "REQUEST_UNPICKED"
        assert pending.review_id == sent.review_id
        assert pending.verdict is None
        assert "No verdict yet" in pending.next_action
        assert "Bash sleep 20" in pending.next_action
        assert "every pending response requires continued work or another sleep and poll" in pending.next_action
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
        "Coordinator", "principal-b", "chat-b-replacement",
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
            connection, subject_id, revision, "CODE", None,
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
            connection, subject_id, current_revision, "CODE", None,
        ) == "NOT_PASS"


async def test_late_findings_are_saved_for_focused_rereview(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a",
    )).mailbox
    reviewer = (await register_coordinator(
        mailboxes, "principal-b", "chat-b",
    )).mailbox
    assert requester is not None and reviewer is not None
    service = ReviewService(
        occurrences, mailboxes, messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    old_revision = canonical_revision(subject.work_id, subject.row_version)
    sent = await service.request(ReviewRequest(
        subject_work_id=subject_id, observed_revision=old_revision,
        review_kind="CODE",
    ), requester)
    assert sent.review_id is not None and sent.delivery_id is not None
    assert (await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(generation="1", current_generation="1"),
        MessageReceiveRequest(
            api_version="1", delivery_id=sent.delivery_id, grant_version=1,
        ),
        agent_binding=reviewer,
    )).status == "ok"
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE canonical_work SET notes = 'advanced', row_version = row_version + 1 "
            "WHERE work_id = :id"
        ), {"id": subject_id})
    finding = ReviewFinding(
        finding_id="F-late", defect="Missing proof", evidence="No boundary result",
        consequence="Claim is unsafe", affected_claim="landing",
        minimum_clearing_condition="Supply boundary proof",
    )

    stale = await service.submit(ReviewSubmit(
        review_id=sent.review_id, verdict="FINDINGS", findings=(finding,),
        context_provenance="UNSEEDED",
    ), reviewer)

    assert (stale.status, stale.reason) == ("STALE", "subject_revision_changed")
    outcomes = await occurrences.outcome_envelopes(sent.review_id)
    assert len(outcomes) == 1
    assert outcomes[0].brief.basis.subject_revision == old_revision
    assert outcomes[0].outcome.findings == (finding,)
    projected = await service.get(sent.review_id, requester)
    assert (projected.status, projected.reason) == ("STALE", "review_basis_changed")
    occurrence = await occurrences.works.get(sent.review_id)
    assert occurrence is not None and occurrence.completed is False
    current = await occurrences.works.get(subject_id)
    assert current is not None
    focused = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(current.work_id, current.row_version),
        review_kind="CODE", mode="FOCUSED", prior_review_id=sent.review_id,
        finding_ids=(finding.finding_id,),
    ), requester)
    assert focused.status == "SENT" and focused.delivery_id is not None
    assert (await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(generation="1", current_generation="1"),
        MessageReceiveRequest(
            api_version="1", delivery_id=focused.delivery_id, grant_version=1,
        ),
        agent_binding=reviewer,
    )).status == "ok"
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE canonical_work SET row_version = row_version + 1 WHERE work_id = :id"
        ), {"id": subject_id})
    unselected = finding.model_copy(update={"finding_id": "F-unselected"})
    rejected = await service.submit(ReviewSubmit(
        review_id=sent.review_id, verdict="FINDINGS", findings=(unselected,),
        context_provenance="UNSEEDED",
    ), reviewer)
    assert (rejected.status, rejected.reason) == (
        "DENIED", "focused_finding_not_selected",
    )
    assert len(await occurrences.outcome_envelopes(sent.review_id)) == 1


@pytest.mark.parametrize("verdict", ["PASS", "BLOCKED"])
async def test_late_nonfinding_verdict_is_dropped(occurrence_runtime, verdict):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a",
    )).mailbox
    reviewer = (await register_coordinator(
        mailboxes, "principal-b", "chat-b",
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
    assert (await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(generation="1", current_generation="1"),
        MessageReceiveRequest(
            api_version="1", delivery_id=sent.delivery_id, grant_version=1,
        ),
        agent_binding=reviewer,
    )).status == "ok"
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE canonical_work SET row_version = row_version + 1 WHERE work_id = :id"
        ), {"id": subject_id})

    stale = await service.submit(ReviewSubmit(
        review_id=sent.review_id, verdict=verdict, context_provenance="UNSEEDED",
    ), reviewer)

    assert (stale.status, stale.reason) == ("STALE", "subject_revision_changed")
    assert await occurrences.outcome_envelopes(sent.review_id) == ()


async def test_pass_status_requires_exact_review_kind_and_candidate(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a"
    )).mailbox
    reviewer = (await register_coordinator(
        mailboxes, "principal-b", "chat-b"
    )).mailbox
    assert requester is not None and reviewer is not None
    service = ReviewService(
        occurrences, mailboxes, messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    revision = canonical_revision(subject.work_id, subject.row_version)
    sent = await service.request(ReviewRequest(
        subject_work_id=subject_id, observed_revision=revision,
        review_kind="CODE", candidate_ref="git:exact",
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
    async with engine.begin() as connection:
        assert await occurrences.pass_status_in_transaction(
            connection, subject_id, revision, "IMPLEMENTATION", "git:exact",
        ) == "NOT_PASS"
        assert await occurrences.pass_status_in_transaction(
            connection, subject_id, revision, "CODE", "git:other",
        ) == "NOT_PASS"
        assert await occurrences.pass_status_in_transaction(
            connection, subject_id, revision, "CODE", "git:exact",
        ) == "PASS"


async def test_recovery_rejects_concluded_historical_delivery(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a"
    )).mailbox
    reviewer = (await register_coordinator(
        mailboxes, "principal-b", "chat-b"
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
        "Coordinator", "principal-b", "chat-b-replacement",
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
    reviewer = (await register_coordinator(
        mailboxes, "principal-b", "chat-b"
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
    reviewer = (await register_coordinator(
        mailboxes, "principal-reviewer", "chat-reviewer"
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
    occurrences.policy = ReviewPolicy(version="policy-v1")
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
    reviewer = (await register_coordinator(
        mailboxes, "principal-b", "chat-b"
    )).mailbox
    assert reviewer is not None
    service.policy = ReviewPolicy(version="policy-v1")
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
            "CODE", None,
        ) == "PASS"


async def test_current_owner_can_poll_after_mailbox_takeover(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, _engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a"
    )).mailbox
    reviewer = (await register_coordinator(
        mailboxes, "principal-b", "chat-b"
    )).mailbox
    other = (await mailboxes.register_agent(
        "Other", "principal-c", "chat-c"
    )).mailbox
    assert requester is not None and reviewer is not None and other is not None
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
    assert sent.status == "SENT" and sent.review_id is not None

    replacement = (await mailboxes.takeover(
        requester.name, requester.principal_key, "replacement-chat"
    )).mailbox
    assert replacement is not None
    assert replacement.endpoint_id == requester.endpoint_id
    assert replacement.generation == requester.generation + 1
    assert (await service.get(sent.review_id, replacement)).status == "REQUEST_UNPICKED"
    displaced = await service.get(sent.review_id, requester)
    assert (displaced.status, displaced.reason) == ("DENIED", "caller_not_owner")
    denied = await service.get(sent.review_id, other)
    assert (denied.status, denied.reason) == ("DENIED", "caller_not_owner")


async def test_focused_rereview_allows_only_authoritative_named_findings(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a"
    )).mailbox
    reviewer = (await register_coordinator(
        mailboxes, "principal-b", "chat-b"
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

    focused_received = await messages.receive_admitted(
        reviewer.endpoint_id, reviewer.generation,
        RuntimeCurrentness(
            generation=str(reviewer.generation), current_generation=str(reviewer.generation),
        ),
        MessageReceiveRequest(
            api_version="1", delivery_id=focused.delivery_id,
            grant_version=reviewer.generation,
        ),
        agent_binding=reviewer,
    )
    assert focused_received.status == "ok"
    new_finding = finding.model_copy(update={"finding_id": "F-2"})
    rejected = await service.submit(ReviewSubmit(
        review_id=initial.review_id, verdict="FINDINGS", findings=(new_finding,),
        context_provenance="UNSEEDED",
    ), reviewer)
    assert (rejected.status, rejected.reason) == (
        "DENIED", "focused_finding_not_selected",
    )

    upgraded = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(corrected.work_id, corrected.row_version),
        review_kind="CODE", prior_review_id=initial.review_id,
    ), requester)
    assert (upgraded.status, upgraded.reason) == ("UNKNOWN", "duplicate_authoritative_state")
    assert (await service.recover(initial.review_id, reviewer)).reason == "duplicate_authoritative_state"
    assert (await service.bundle_access(initial.review_id, reviewer)).reason == (
        "duplicate_authoritative_state"
    )


@pytest.mark.parametrize(
    "review_kind",
    ("CODE", "DESIGN", "IMPLEMENTATION", "PROCESS", "OPERATIONS", "EVIDENCE"),
)
async def test_every_review_kind_is_delivered_to_coordinator(occurrence_runtime, review_kind):
    occurrences, _messages, mailboxes, subject_id, _engine = occurrence_runtime
    coordinator = (await register_coordinator(
        mailboxes, "principal-c", "chat-c",
    )).mailbox
    other = (await mailboxes.register_agent("Reviewer", "principal-b", "chat-b")).mailbox
    requester = (await mailboxes.register_agent(
        "Requester", "principal-a", "chat-a",
    )).mailbox
    assert coordinator is not None and other is not None and requester is not None
    service = ReviewService(
        occurrences, mailboxes, _messages, occurrences.policy,
        ReviewGuidelines(version="guidelines-v1", digest="a" * 64),
    )
    subject = await occurrences.works.get(subject_id)
    assert subject is not None
    sent = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(subject_id, subject.row_version),
        review_kind=review_kind,
        candidate_ref="github:marcogallotta/switchstand:pr/7",
    ), requester)
    assert sent.status == "SENT" and sent.review_id is not None, sent
    deliveries = await occurrences.request_deliveries(sent.review_id)
    assert [delivery.recipient_work_id for delivery, _envelope in deliveries] == [
        coordinator.endpoint_id,
    ]


async def test_coordinator_bundle_access_is_fenced(occurrence_runtime):
    occurrences, messages, mailboxes, subject_id, engine = occurrence_runtime
    coordinator = (await register_coordinator(
        mailboxes, "principal-c", "chat-c",
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

    async def pick_up(delivery_id: UUID) -> None:
        received = await messages.receive_admitted(
            coordinator.endpoint_id, coordinator.generation,
            RuntimeCurrentness(generation="1", current_generation="1"),
            MessageReceiveRequest(api_version="1", delivery_id=delivery_id,
                                  grant_version=coordinator.generation),
            agent_binding=coordinator,
        )
        assert received.status == "ok"

    sent = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(subject_id, subject.row_version),
        review_kind="CODE",
        candidate_ref="github:marcogallotta/switchstand:pr/7",
    ), requester)
    assert sent.status == "SENT" and sent.delivery_id is not None and sent.review_id
    assert (await service.bundle_access(sent.review_id, coordinator)).status == "DENIED"
    await pick_up(sent.delivery_id)
    access = await service.bundle_access(sent.review_id, coordinator)
    assert access.status == "AUTHORIZED" and access.basis is not None
    assert (await service.bundle_access(
        sent.review_id, coordinator, access.basis,
    )).status == "READY"
    unsupported = await service.request(ReviewRequest(
        subject_work_id=subject_id,
        observed_revision=canonical_revision(subject_id, subject.row_version),
        review_kind="DESIGN", candidate_ref="github:marcogallotta/switchstand:pr/8",
    ), requester)
    assert unsupported.delivery_id is not None and unsupported.review_id is not None
    await pick_up(unsupported.delivery_id)
    unsupported_access = await service.bundle_access(unsupported.review_id, coordinator)
    assert (unsupported_access.status, unsupported_access.reason) == (
        "UNKNOWN", "REVIEW_KIND_UNSUPPORTED_V1",
    )
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE canonical_work SET row_version = row_version + 1 WHERE work_id = :id"
        ), {"id": subject_id})
    assert (await service.bundle_access(
        sent.review_id, coordinator, access.basis,
    )).status == "STALE"
    stale_get = await service.get(sent.review_id, requester)
    assert (stale_get.status, stale_get.reason) == ("STALE", "review_basis_changed")
    stale_recover = await service.recover(sent.review_id, coordinator)
    assert (stale_recover.status, stale_recover.reason) == (
        "STALE", "review_basis_changed",
    )
