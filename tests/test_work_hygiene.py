import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_metadata,
    canonical_revision,
)
from switchstand.contracts import LaunchAuthority
from switchstand.durable_capture import AcceptedFindingRoute, FindingCapture, FindingRef
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext, WorkGrant
from switchstand.messages import MessageState, message_deliveries, messages
from switchstand.reviews import ReviewService
from switchstand.state import metadata
from switchstand.task_control import (
    AttributableCorrection,
    DurableControlCapsule,
    TaskControlCheckpoint,
    TaskControlReadResult,
    TaskControlState,
)
from switchstand.work_hygiene import WorkHygieneState, evaluate_work_hygiene


def work() -> CurrentWork:
    work_id = uuid4()
    return CurrentWork(
        work_id, "Implement", False, "progress", priority="HIGH", work_type="Task",
        lifecycle_state="CURRENT", canonical_root=str(work_id), owner_key="agent:root",
        wait_kind="NONE", unblock_condition="NONE", next_due="NONE",
        next_action_class="OWNER_CAN_DO", next_action_ref="implement",
    )


def checkpoint(item: CurrentWork, corrections=(), targets=()) -> TaskControlReadResult:
    capsule = DurableControlCapsule(
        objective="Implement", completion_condition="Tests pass",
        success_proof="CI receipt", target_refs=targets,
        intended_effect_class="repository", progress_summary="in progress",
        progress_evidence_ref="git:head", applicable_corrections=corrections,
    )
    value = TaskControlCheckpoint(
        checkpoint_id=uuid4(), work_id=item.work_id, generation=1,
        observed_work_revision=canonical_revision(item.work_id, item.row_version),
        control_basis_digest="a" * 64, content_digest="b" * 64,
        capsule=capsule, created_at=datetime.now(UTC),
    )
    return TaskControlReadResult(
        status="ok", currentness="CURRENT", reason="checkpoint_current",
        checkpoint=value, current_work_revision=value.observed_work_revision,
    )


def evaluate(item: CurrentWork, **changes):
    values = {
        "work": item, "work_id": item.work_id,
        "observed_revision": canonical_revision(item.work_id, item.row_version),
        "gate": "HANDOFF", "checkpoint": checkpoint(item),
        "finding_refs": (), "watch_refs": (), "unknown_effect_refs": (),
        "invalid_capture_refs": (),
    }
    values.update(changes)
    return evaluate_work_hygiene(**values)


def test_complete_current_and_waiting_work_passes() -> None:
    item = work()
    assert evaluate(item).status == "PASS"
    waiting = replace(
        item, lifecycle_state="WAITING", wait_kind="HUMAN",
        unblock_condition="Marco answers", next_due="2026-10-08",
        next_action_class="NONE", next_action_ref="NONE",
    )
    assert evaluate(waiting, checkpoint=checkpoint(waiting)).status == "PASS"


def test_stale_missing_and_unknown_evidence_never_passes() -> None:
    item = work()
    stale = evaluate(item, observed_revision="stale")
    assert stale.status == "UNKNOWN" and stale.currentness == "STALE"
    assert evaluate(item, checkpoint=None).status == "UNKNOWN"
    assert evaluate(item, unknown_effect_refs=("operation:o1",)).status == "UNKNOWN"
    malformed = replace(item, lifecycle_state="CURRENT", wait_kind="HUMAN")
    assert evaluate(malformed, checkpoint=checkpoint(malformed)).status == "FAIL"


def test_findings_and_handoff_watches_require_durable_capture() -> None:
    item = work()
    ref = FindingRef(source_ref="review:r1", finding_id="F1")
    assert evaluate(item, finding_refs=(ref,)).status == "FAIL"
    accepted = FindingCapture(
        finding_id="F1", disposition="ACCEPT",
        accepted_route=AcceptedFindingRoute(kind="LINK_EXISTING", work_id=uuid4()),
    )
    correction = AttributableCorrection(
        summary="accepted", source_ref="review:r1", finding_capture=accepted,
    )
    recorded = checkpoint(item, corrections=(correction,), targets=("message:m1",))
    assert evaluate(
        item, checkpoint=recorded, finding_refs=(ref,), watch_refs=("message:m1",),
    ).status == "PASS"
    assert evaluate(
        item, checkpoint=recorded, finding_refs=(ref,),
        invalid_capture_refs=("review:r1#F1",),
    ).status == "FAIL"
    assert evaluate(item, checkpoint=recorded, finding_refs=(ref,),
                    watch_refs=("message:m2",)).status == "FAIL"
    assert evaluate(item, checkpoint=recorded, finding_refs=(ref,),
                    watch_refs=("message:m1",), gate="ASSIGNMENT_COMPLETE").status == "FAIL"


def test_hygiene_digest_is_order_independent_for_evidence() -> None:
    item = work()
    current = checkpoint(item)
    first = evaluate(item, checkpoint=current, watch_refs=("message:b", "message:a"))
    second = evaluate(item, checkpoint=current, watch_refs=("message:a", "message:b"))
    assert first.basis_digest == second.basis_digest


@pytest.mark.asyncio
async def test_persistent_hygiene_validates_accepted_routes(
    database_prerequisite: None,
) -> None:
    del database_prerequisite
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.run_sync(canonical_metadata.create_all)
    works, grants = CanonicalWorkRepository(engine), GrantState(engine)
    subject, linked, other = work(), work(), work()
    linked = replace(linked, owner_key=subject.owner_key)
    other = replace(other, owner_key="agent:other")
    for item in (subject, linked, other):
        await works.create(item)
    principal = PrincipalContext(
        issuer="test", subject=str(uuid4()), client_id="test", assurance="test",
    )
    grant = WorkGrant(
        id=uuid4(), version=1, principal=principal,
        authority=LaunchAuthority(active_work_id=subject.work_id), scope="launch",
        operations=frozenset({"work_get", "task_control"}), issuer="test",
        provenance="test", expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    state = TaskControlState(engine, works)
    finding = FindingRef(source_ref="review:r1", finding_id="F1")

    class Findings:
        async def hygiene_findings(
            self, _work_id: UUID, _connection,
        ) -> tuple[FindingRef, ...]:
            return (finding,)

    message_state = MessageState(engine, grants)
    hygiene = WorkHygieneState(
        works, state, grants, message_state, cast(ReviewService, Findings()),
    )

    async def result(route: AcceptedFindingRoute) -> str:
        current = await works.get(subject.work_id)
        assert current is not None
        prior = await state.read(subject.work_id)
        expected = (
            None if prior.checkpoint is None else prior.checkpoint.generation
        )
        correction = AttributableCorrection(
            summary="accepted", source_ref="review:r1",
            finding_capture=FindingCapture(
                finding_id="F1", disposition="ACCEPT", accepted_route=route,
            ),
        )
        saved = await state.checkpoint(
            principal, grant, uuid4(), subject.work_id,
            canonical_revision(subject.work_id, current.row_version), expected,
            DurableControlCapsule(
                objective="Implement", completion_condition="Tests pass",
                success_proof="CI", intended_effect_class="repository",
                progress_summary="in progress", progress_evidence_ref="git:head",
                applicable_corrections=(correction,),
            ),
        )
        assert saved.status == "ok"
        checked = await hygiene.check(
            subject.work_id, canonical_revision(subject.work_id, current.row_version),
            "HANDOFF",
        )
        return checked.status

    assert await result(AcceptedFindingRoute(
        kind="LINK_EXISTING", work_id=linked.work_id,
    )) == "PASS"
    assert await result(AcceptedFindingRoute(
        kind="LINK_EXISTING", work_id=uuid4(),
    )) == "FAIL"
    assert await result(AcceptedFindingRoute(
        kind="CREATE_SELF_OWNED", work_id=other.work_id,
    )) == "FAIL"

    delivery_id, message_id = uuid4(), uuid4()
    async with engine.begin() as connection:
        await connection.execute(insert(messages).values(
            sender_work_id=subject.work_id, message_id=message_id,
            route_ref="owner.adoption", kind="request",
            payload={
                "work_id": str(subject.work_id), "proposed_owner": "agent:other",
            },
            digest="d" * 64,
        ))
        await connection.execute(insert(message_deliveries).values(
            delivery_id=delivery_id, sender_work_id=subject.work_id,
            message_id=message_id, recipient_work_id=other.work_id,
            state="DISPOSITIONED", recipient_grant_version=1,
        ))
    assert await result(AcceptedFindingRoute(
        kind="ROUTE_PROPOSED_OWNER", owner_ref="agent:other",
        proposal_ref=f"message:{delivery_id}",
    )) == "FAIL"
    async with engine.begin() as connection:
        await connection.run_sync(canonical_metadata.drop_all)
    await engine.dispose()
