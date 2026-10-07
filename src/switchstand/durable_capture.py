"""Typed guidance for durably dispositioning review and message findings."""

from collections.abc import Iterable
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from .contracts import ClosedModel

FindingDisposition = Literal["ACCEPT", "CHALLENGE", "NARROW", "NEEDS_EVIDENCE"]
CaptureState = Literal[
    "NOT_APPLICABLE", "DECISION_REQUIRED", "REQUIRED", "SATISFIED", "BLOCKED",
]


class AcceptedFindingRoute(ClosedModel):
    kind: Literal["LINK_EXISTING", "CREATE_SELF_OWNED", "ROUTE_PROPOSED_OWNER"]
    work_id: UUID | None = None
    owner_ref: str | None = Field(default=None, min_length=1, max_length=2048)
    proposal_ref: str | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def exact_route(self) -> AcceptedFindingRoute:
        if self.kind in {"LINK_EXISTING", "CREATE_SELF_OWNED"}:
            if self.work_id is None or self.owner_ref is not None or self.proposal_ref is not None:
                raise ValueError("linked or self-owned route requires only an exact WorkId")
        elif self.work_id is not None or self.owner_ref is None or self.proposal_ref is None:
            raise ValueError("proposed-owner route requires owner and proposal references")
        return self


class FindingCapture(ClosedModel):
    finding_id: str = Field(min_length=1, max_length=200)
    disposition: FindingDisposition
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=32)
    accepted_route: AcceptedFindingRoute | None = None

    @model_validator(mode="after")
    def exact_disposition(self) -> FindingCapture:
        if any(not value or len(value) > 2048 for value in self.evidence_refs):
            raise ValueError("finding evidence references must be nonempty and bounded")
        if self.disposition == "ACCEPT":
            return self
        if self.accepted_route is not None or not self.evidence_refs:
            raise ValueError("non-accepted findings require evidence and forbid an accepted route")
        return self


class FindingRef(ClosedModel):
    source_ref: str = Field(min_length=1, max_length=2048)
    finding_id: str = Field(min_length=1, max_length=200)


class DurableCaptureGuidance(ClosedModel):
    state: CaptureState
    finding_refs: tuple[FindingRef, ...] = ()
    unresolved: tuple[FindingRef, ...] = ()
    next_action: str
    reason: str | None = None


class CorrectionView(Protocol):
    source_ref: str
    finding_capture: FindingCapture | None


def durable_capture_guidance(
    finding_refs: Iterable[FindingRef], corrections: Iterable[CorrectionView],
) -> DurableCaptureGuidance:
    refs = tuple(sorted(finding_refs, key=lambda item: (item.source_ref, item.finding_id)))
    if not refs:
        return DurableCaptureGuidance(
            state="NOT_APPLICABLE", next_action="No typed findings require capture.",
        )
    expected = {(item.source_ref, item.finding_id): item for item in refs}
    captured: dict[tuple[str, str], FindingCapture] = {}
    try:
        for correction in corrections:
            capture = correction.finding_capture
            if capture is None:
                continue
            source_ref = correction.source_ref
            key = (source_ref, capture.finding_id)
            if key not in expected or key in captured:
                return DurableCaptureGuidance(
                    state="BLOCKED", finding_refs=refs, next_action="Reconcile conflicting capture.",
                    reason="duplicate_or_unknown_capture",
                )
            captured[key] = capture
    except (AttributeError, TypeError, ValueError):
        return DurableCaptureGuidance(
            state="BLOCKED", finding_refs=refs, next_action="Repair malformed capture evidence.",
            reason="malformed_capture",
        )
    missing = tuple(value for key, value in expected.items() if key not in captured)
    if missing:
        return DurableCaptureGuidance(
            state="DECISION_REQUIRED", finding_refs=refs, unresolved=missing,
            next_action="Disposition every typed finding.",
        )
    unrouted = tuple(
        expected[key] for key, value in captured.items()
        if value.disposition == "ACCEPT" and value.accepted_route is None
    )
    if unrouted:
        return DurableCaptureGuidance(
            state="REQUIRED", finding_refs=refs, unresolved=unrouted,
            next_action=(
                "Link accepted findings to existing work, created self-owned work, "
                "or an attributable proposed-owner route."
            ),
        )
    return DurableCaptureGuidance(
        state="SATISFIED", finding_refs=refs,
        next_action="Use the checkpointed finding dispositions.",
    )


def message_capture_guidance(payload: JsonValue) -> DurableCaptureGuidance:
    if not isinstance(payload, dict):
        return durable_capture_guidance((), ())
    candidate = payload
    outcome = payload.get("outcome")
    if isinstance(outcome, dict) and outcome.get("type") == "REVIEW_OUTCOME":
        candidate = outcome
    kind = candidate.get("type")
    if kind not in {"FINDINGS", "REVIEW_OUTCOME"}:
        return durable_capture_guidance((), ())
    if kind == "REVIEW_OUTCOME" and candidate.get("verdict") != "FINDINGS":
        return durable_capture_guidance((), ())
    source_ref = candidate.get("source_ref")
    if source_ref is None and candidate.get("review_id") is not None:
        source_ref = f"review:{candidate['review_id']}"
    findings = candidate.get("findings")
    if not isinstance(source_ref, str) or not isinstance(findings, list):
        return DurableCaptureGuidance(
            state="BLOCKED", next_action="Repair the malformed typed finding envelope.",
            reason="malformed_finding_envelope",
        )
    try:
        refs = tuple(FindingRef(
            source_ref=source_ref,
            finding_id=str(value["finding_id"]),
        ) for value in findings if isinstance(value, dict))
        if len(refs) != len(findings):
            raise ValueError("finding shape")
    except (KeyError, TypeError, ValueError):
        return DurableCaptureGuidance(
            state="BLOCKED", next_action="Repair the malformed typed finding envelope.",
            reason="malformed_finding_envelope",
        )
    return durable_capture_guidance(refs, ())
