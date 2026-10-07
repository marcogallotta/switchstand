"""Deterministic read-only readiness checks over existing canonical owners."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .canonical_work import CanonicalWorkRepository, CurrentWork, canonical_revision
from .contracts import ClosedModel
from .durable_capture import FindingRef, durable_capture_guidance
from .grant_state import GrantState
from .messages import MessageState
from .reviews import ReviewService
from .task_control import TaskControlReadResult, TaskControlState
from .work_policy import validate_resultant_state

HygieneGate = Literal[
    "HANDOFF", "ASSIGNMENT_COMPLETE", "READY_FOR_REVIEW", "RESUME_REENTRY",
    "TYPED_FINDINGS",
]
HygieneState = Literal["PASS", "FAIL", "UNKNOWN"]
HygieneCheckName = Literal[
    "lifecycle_action_or_wait", "scope_completeness", "checkpoint_currentness",
    "review_finding_disposition", "handoff_watches", "accepted_finding_capture",
    "unknown_effects",
]


class HygieneCheck(ClosedModel):
    name: HygieneCheckName
    state: HygieneState
    reason: str
    refs: tuple[str, ...] = ()


class WorkHygieneResult(ClosedModel):
    status: HygieneState
    currentness: Literal["CURRENT", "STALE", "UNKNOWN"]
    work_id: UUID
    observed_revision: str
    current_revision: str | None = None
    basis_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: tuple[HygieneCheck, ...]


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _check(
    name: HygieneCheckName, state: HygieneState, reason: str,
    refs: tuple[str, ...] = (),
) -> HygieneCheck:
    return HygieneCheck(name=name, state=state, reason=reason, refs=tuple(sorted(refs)))


def evaluate_work_hygiene(
    *, work: CurrentWork | None, work_id: UUID, observed_revision: str,
    gate: HygieneGate, checkpoint: TaskControlReadResult | None,
    finding_refs: tuple[FindingRef, ...] | None,
    watch_refs: tuple[str, ...] | None, unknown_effect_refs: tuple[str, ...] | None,
    invalid_capture_refs: tuple[str, ...] | None = None,
) -> WorkHygieneResult:
    if work is None:
        return WorkHygieneResult(
            status="UNKNOWN", currentness="UNKNOWN", work_id=work_id,
            observed_revision=observed_revision,
            basis_digest=_digest({"work_id": str(work_id), "reason": "work_unavailable"}),
            checks=(_check("scope_completeness", "UNKNOWN", "work_unavailable"),),
        )
    current_revision = canonical_revision(work_id, work.row_version)
    if current_revision != observed_revision:
        return WorkHygieneResult(
            status="UNKNOWN", currentness="STALE", work_id=work_id,
            observed_revision=observed_revision, current_revision=current_revision,
            basis_digest=_digest({
                "work_id": str(work_id), "observed": observed_revision,
                "current": current_revision, "gate": gate,
            }),
            checks=(_check("scope_completeness", "UNKNOWN", "work_revision_changed"),),
        )
    checks: list[HygieneCheck] = []
    try:
        validate_resultant_state(
            lifecycle_state=work.lifecycle_state, canonical_root=work.canonical_root,
            owner_key=work.owner_key, wait_kind=work.wait_kind,
            unblock_condition=work.unblock_condition, next_due=work.next_due,
            next_action_class=work.next_action_class, next_action_ref=work.next_action_ref,
        )
        lifecycle = "UNKNOWN" if work.lifecycle_state == "UNKNOWN" else "PASS"
        checks.append(_check(
            "lifecycle_action_or_wait", lifecycle,
            "lifecycle_unknown" if lifecycle == "UNKNOWN" else "action_or_wait_complete",
        ))
    except ValueError:
        checks.append(_check("lifecycle_action_or_wait", "FAIL", "incoherent_routing"))
    scope_values = (work.work_type, work.canonical_root, work.owner_key)
    if any(value is None or value == "NONE" for value in scope_values):
        checks.append(_check("scope_completeness", "FAIL", "scope_incomplete"))
    elif any(value == "UNKNOWN" for value in scope_values):
        checks.append(_check("scope_completeness", "UNKNOWN", "scope_explicitly_unknown"))
    else:
        checks.append(_check("scope_completeness", "PASS", "scope_complete"))
    corrections = ()
    if checkpoint is None:
        checks.append(_check("checkpoint_currentness", "UNKNOWN", "checkpoint_unavailable"))
    elif checkpoint.currentness == "CURRENT" and checkpoint.checkpoint is not None:
        checks.append(_check("checkpoint_currentness", "PASS", "checkpoint_current"))
        corrections = checkpoint.checkpoint.capsule.applicable_corrections
    elif checkpoint.currentness == "STALE":
        checks.append(_check("checkpoint_currentness", "FAIL", "checkpoint_stale"))
    else:
        checks.append(_check("checkpoint_currentness", "UNKNOWN", checkpoint.reason))
    if finding_refs is None:
        checks.append(_check(
            "review_finding_disposition", "UNKNOWN", "review_evidence_unavailable",
        ))
        checks.append(_check(
            "accepted_finding_capture", "UNKNOWN", "review_evidence_unavailable",
        ))
    else:
        guidance = durable_capture_guidance(finding_refs, corrections)
        capture_state: HygieneState
        capture_reason = guidance.state
        if guidance.state == "NOT_APPLICABLE":
            capture_state = "PASS"
        elif invalid_capture_refs is None:
            capture_state, capture_reason = "UNKNOWN", "capture_evidence_unavailable"
        elif invalid_capture_refs:
            capture_state, capture_reason = "FAIL", "capture_route_not_authoritative"
        else:
            capture_state = "PASS" if guidance.state == "SATISFIED" else "FAIL"
        refs = tuple(f"{item.source_ref}#{item.finding_id}" for item in guidance.unresolved)
        refs = tuple(sorted((*refs, *(invalid_capture_refs or ()))))
        checks.append(_check(
            "review_finding_disposition", capture_state, capture_reason, refs,
        ))
        checks.append(_check("accepted_finding_capture", capture_state, capture_reason, refs))
    if watch_refs is None:
        checks.append(_check("handoff_watches", "UNKNOWN", "watch_evidence_unavailable"))
    else:
        recorded: set[str] = set()
        if checkpoint is not None and checkpoint.checkpoint is not None:
            recorded = set(checkpoint.checkpoint.capsule.target_refs)
        missing = tuple(ref for ref in watch_refs if ref not in recorded)
        if gate in {"ASSIGNMENT_COMPLETE", "READY_FOR_REVIEW"} and watch_refs:
            checks.append(_check("handoff_watches", "FAIL", "open_watches", watch_refs))
        elif gate == "HANDOFF" and missing:
            checks.append(_check("handoff_watches", "FAIL", "unrecorded_watches", missing))
        else:
            checks.append(_check("handoff_watches", "PASS", "watches_preserved", watch_refs))
    if unknown_effect_refs is None:
        checks.append(_check("unknown_effects", "UNKNOWN", "effect_evidence_unavailable"))
    elif unknown_effect_refs:
        checks.append(_check("unknown_effects", "UNKNOWN", "unresolved_effects", unknown_effect_refs))
    else:
        checks.append(_check("unknown_effects", "PASS", "no_unresolved_effects"))
    status: HygieneState = (
        "FAIL" if any(value.state == "FAIL" for value in checks)
        else "UNKNOWN" if any(value.state == "UNKNOWN" for value in checks) else "PASS"
    )
    evidence = {
        "gate": gate, "work_id": str(work_id), "revision": current_revision,
        "routing": {field: getattr(work, field) for field in (
            "work_type", "lifecycle_state", "canonical_root", "owner_key", "wait_kind",
            "unblock_condition", "next_due", "next_action_class", "next_action_ref",
        )},
        "checkpoint": None if checkpoint is None else checkpoint.model_dump(mode="json"),
        "findings": [value.model_dump(mode="json") for value in (finding_refs or ())],
        "invalid_capture_refs": sorted(invalid_capture_refs or ()),
        "watches": sorted(watch_refs or ()), "unknown_effects": sorted(unknown_effect_refs or ()),
    }
    return WorkHygieneResult(
        status=status, currentness="CURRENT", work_id=work_id,
        observed_revision=observed_revision, current_revision=current_revision,
        basis_digest=_digest(evidence), checks=tuple(checks),
    )


class WorkHygieneState:
    def __init__(
        self, works: CanonicalWorkRepository, task_control: TaskControlState,
        grants: GrantState, messages: MessageState | None = None,
        reviews: ReviewService | None = None,
    ):
        self.works, self.task_control, self.grants = works, task_control, grants
        self.messages, self.reviews = messages, reviews

    async def check(
        self, work_id: UUID, observed_revision: str, gate: HygieneGate,
    ) -> WorkHygieneResult:
        async with (
            self.task_control.engine.connect() as connection,
            connection.begin(),
        ):
            await connection.execute(text(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            ))
            work = await self.works.get_in(connection, work_id)
            checkpoint = await self.task_control.read_in(connection, work_id)
            findings = (
                None if self.reviews is None
                else await self.reviews.hygiene_findings(work_id, connection)
            )
            watches = (
                None if self.messages is None
                else await self.messages.open_handoff_watches(work_id, connection)
            )
            effects = await self.grants.unresolved_effect_ids(work_id, connection)
            invalid = await self._invalid_capture_refs(connection, work, checkpoint)
            return evaluate_work_hygiene(
                work=work, work_id=work_id, observed_revision=observed_revision,
                gate=gate, checkpoint=checkpoint, finding_refs=findings,
                watch_refs=watches, unknown_effect_refs=effects,
                invalid_capture_refs=invalid,
            )

    async def _invalid_capture_refs(
        self, connection: AsyncConnection, work: CurrentWork | None,
        checkpoint: TaskControlReadResult,
    ) -> tuple[str, ...] | None:
        if work is None or checkpoint.checkpoint is None:
            return ()
        invalid: list[str] = []
        for correction in checkpoint.checkpoint.capsule.applicable_corrections:
            capture = correction.finding_capture
            if capture is None or capture.disposition != "ACCEPT" or capture.accepted_route is None:
                continue
            route = capture.accepted_route
            reference = f"{correction.source_ref}#{capture.finding_id}"
            if route.kind in {"LINK_EXISTING", "CREATE_SELF_OWNED"}:
                assert route.work_id is not None
                linked = await self.works.get_in(connection, route.work_id)
                if linked is None or (
                    route.kind == "CREATE_SELF_OWNED" and linked.owner_key != work.owner_key
                ):
                    invalid.append(reference)
            else:
                if self.messages is None:
                    return None
                assert route.proposal_ref is not None and route.owner_ref is not None
                if not await self.messages.has_open_owner_proposal(
                    connection, work_id=work.work_id,
                    proposal_ref=route.proposal_ref, owner_ref=route.owner_ref,
                ):
                    invalid.append(reference)
        return tuple(sorted(invalid))
