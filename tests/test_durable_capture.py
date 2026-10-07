from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from switchstand.durable_capture import (
    AcceptedFindingRoute,
    FindingCapture,
    FindingRef,
    durable_capture_guidance,
    message_capture_guidance,
)


def correction(source: str, capture: FindingCapture) -> SimpleNamespace:
    return SimpleNamespace(source_ref=source, finding_capture=capture)


def test_capture_guidance_covers_all_required_states() -> None:
    ref = FindingRef(source_ref="review:r1", finding_id="F1")
    assert durable_capture_guidance((), ()).state == "NOT_APPLICABLE"
    assert durable_capture_guidance((ref,), ()).state == "DECISION_REQUIRED"

    accepted = FindingCapture(finding_id="F1", disposition="ACCEPT")
    assert durable_capture_guidance(
        (ref,), (correction("review:r1", accepted),),
    ).state == "REQUIRED"

    routed = accepted.model_copy(update={
        "accepted_route": AcceptedFindingRoute(
            kind="LINK_EXISTING", work_id=uuid4(),
        ),
    })
    assert durable_capture_guidance(
        (ref,), (correction("review:r1", routed),),
    ).state == "SATISFIED"
    assert durable_capture_guidance(
        (ref,), (correction("review:r1", routed), correction("review:r1", routed)),
    ).state == "BLOCKED"


@pytest.mark.parametrize("kind", ["LINK_EXISTING", "CREATE_SELF_OWNED"])
def test_accepted_routes_bind_only_exact_work(kind: str) -> None:
    route = AcceptedFindingRoute(kind=kind, work_id=uuid4())
    assert route.owner_ref is None and route.proposal_ref is None
    with pytest.raises(ValidationError):
        AcceptedFindingRoute(kind=kind, work_id=uuid4(), owner_ref="agent:other")


def test_cross_owner_route_is_proposal_only_and_nonaccept_requires_evidence() -> None:
    route = AcceptedFindingRoute(
        kind="ROUTE_PROPOSED_OWNER", owner_ref="agent:other", proposal_ref="message:m1",
    )
    assert route.work_id is None
    with pytest.raises(ValidationError):
        AcceptedFindingRoute(kind="ROUTE_PROPOSED_OWNER", work_id=uuid4())
    with pytest.raises(ValidationError):
        FindingCapture(finding_id="F1", disposition="CHALLENGE")
    assert FindingCapture(
        finding_id="F1", disposition="NARROW", evidence_refs=("review:r1",),
    ).accepted_route is None


def test_message_guidance_is_typed_and_fail_closed() -> None:
    assert message_capture_guidance({"hello": "world"}).state == "NOT_APPLICABLE"
    assert message_capture_guidance({"type": "FINDINGS"}).state == "BLOCKED"
    guidance = message_capture_guidance({
        "type": "FINDINGS", "source_ref": "message:m1",
        "findings": [{"finding_id": "F1"}],
    })
    assert guidance.state == "DECISION_REQUIRED"
