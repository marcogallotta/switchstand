"""Default-off priority-claim semantics over the durable claim repository."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import Field, model_validator
from sqlalchemy.exc import SQLAlchemyError

from .canonical_work import CanonicalWorkRepository, canonical_revision
from .contracts import ClosedModel
from .grant_state import EffectRecord, GrantState
from .grants import GuardOutcome, PrincipalContext, PriorityClaimReceipt, WorkGrant
from .mutation_effect import PreparedMutation, run_update_or_relation
from .priority_claims import (
    NewPriorityClaim,
    PriorityBand,
    PriorityClaim,
    PriorityClaimRepository,
    RelationKind,
    SubjectKind,
)

CLAIM_NAMESPACE = UUID("43e26bcc-bd6e-446c-baa7-9babe6d15412")


class PriorityClaimWrite(ClosedModel):
    api_version: Literal["1"]
    operation_id: UUID
    work_id: UUID
    grant_version: int = Field(ge=1)
    observed_revision: str = Field(min_length=1)
    relation_kind: RelationKind
    rationale: str = Field(min_length=1, max_length=300)
    relation_target_id: UUID | None = None
    band: PriorityBand | None = None
    supersedes_claim_id: UUID | None = None

    @model_validator(mode="after")
    def valid_relation(self) -> Self:
        valid = (
            (self.relation_kind == "BAND" and self.band is not None
             and self.relation_target_id is None)
            or (self.relation_kind == "BEFORE" and self.band is None
                and self.relation_target_id is not None)
            or (self.relation_kind == "HOLD" and self.band is None
                and self.relation_target_id is None)
        )
        if not valid:
            raise ValueError("priority relation fields do not match relation_kind")
        return self


class PriorityClaimView(ClosedModel):
    claim_id: UUID
    claim_kind: Literal["HUMAN_PRIORITY", "AGENT_RECOMMENDATION"]
    subject_kind: SubjectKind
    subject_id: UUID
    relation_kind: RelationKind
    relation_target_id: UUID | None = None
    band: PriorityBand | None = None
    rationale: str
    source_label: str
    source_ref: str
    source_observed_revision: str | None = None
    supersedes_claim_id: UUID | None = None
    currentness: Literal["CURRENT", "STALE", "UNKNOWN"]


class PriorityClaimReadResult(ClosedModel):
    status: Literal["ok", "denied", "unknown"]
    claims: tuple[PriorityClaimView, ...] = ()
    reason: str | None = None


class PriorityClaimService:
    def __init__(
        self, repository: PriorityClaimRepository, works: CanonicalWorkRepository,
    ):
        self.repository, self.works = repository, works

    @staticmethod
    def guard(
        request: PriorityClaimWrite, status: str, reason: str, *, possible: bool = False,
    ) -> GuardOutcome:
        return GuardOutcome.model_validate({
            "status": status, "operation": "priority_claim_record",
            "work_id": request.work_id, "operation_id": request.operation_id,
            "reason": reason, "effect": "unknown" if possible else "not_sent",
            "retry": "reconcile" if possible else "refresh" if status == "stale" else "none",
            "next_action": (
                "Retry this exact OperationId to reconcile; do not start a new claim effect."
                if possible else "Refresh the exact work/grant or use a trusted issuer."
            ),
        })

    @staticmethod
    def _fingerprint(principal: PrincipalContext, request: PriorityClaimWrite) -> str:
        payload = [principal.key, request.model_dump(mode="json")]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _new(request: PriorityClaimWrite, grant_id: UUID) -> NewPriorityClaim:
        return NewPriorityClaim(
            claim_id=uuid5(CLAIM_NAMESPACE, str(request.operation_id)),
            claim_kind="AGENT_RECOMMENDATION", subject_kind="WORK",
            subject_id=request.work_id, relation_kind=request.relation_kind,
            rationale=request.rationale, source_label="AGENT",
            source_ref=f"grant:{grant_id}",
            source_observed_revision=request.observed_revision,
            relation_target_id=request.relation_target_id, band=request.band,
            supersedes_claim_id=request.supersedes_claim_id,
        )

    @staticmethod
    def _same(stored: PriorityClaim, expected: NewPriorityClaim) -> bool:
        return all(getattr(stored, name) == getattr(expected, name)
                   for name in expected.__dataclass_fields__)

    @staticmethod
    def _applied(
        request: PriorityClaimWrite, principal: PrincipalContext,
        grant_id: UUID, qualification: str, claim: PriorityClaim,
    ) -> GuardOutcome:
        receipt = PriorityClaimReceipt(
            operation_id=request.operation_id, principal=principal,
            grant_id=grant_id, grant_version=request.grant_version,
            work_id=request.work_id, claim_id=claim.claim_id,
            supersedes_claim_id=claim.supersedes_claim_id,
            source_observed_revision=request.observed_revision,
            qualification=qualification,
        )
        return GuardOutcome(
            status="ok", operation="priority_claim_record", work_id=request.work_id,
            operation_id=request.operation_id, reason="priority_claim_converged",
            effect="applied", next_action="Use the recorded receipt.", receipt=receipt,
        )

    async def current(self, kind: SubjectKind, subject_id: UUID) -> PriorityClaimReadResult:
        try:
            claims = await self.repository.current(kind, subject_id)
            work = await self.works.get(subject_id) if kind == "WORK" else None
            revision = None if work is None else canonical_revision(work.work_id, work.row_version)
            return PriorityClaimReadResult(status="ok", claims=tuple(
                PriorityClaimView(
                    **{name: getattr(claim, name) for name in PriorityClaimView.model_fields
                       if name != "currentness"},
                    currentness=(
                        "CURRENT" if claim.source_observed_revision is None
                        else "UNKNOWN" if kind == "PROJECT" or revision is None
                        else "CURRENT" if claim.source_observed_revision == revision else "STALE"
                    ),
                ) for claim in claims
            ))
        except (SQLAlchemyError, TypeError, ValueError):
            return PriorityClaimReadResult(status="unknown", reason="claim_state_unavailable")

    async def record(
        self, grants: GrantState, principal: PrincipalContext, request: PriorityClaimWrite,
    ) -> GuardOutcome:
        return await run_update_or_relation(
            grants, principal, request, self._fingerprint(principal, request),
            "priority_claim", "priority_claim_qualification", "claim_write_not_qualified",
            lambda status, reason, possible: self.guard(
                request, status, "claim_scope_not_granted"
                if reason == "operation_or_work_not_granted" else reason, possible=possible
            ),
            lambda grant, qualification: self._prepare(
                principal, request, grant, qualification
            ),
            lambda record: self._reconcile(grants, principal, request, record),
        )

    async def _prepare(
        self, principal: PrincipalContext, request: PriorityClaimWrite,
        grant: WorkGrant, qualification: str,
    ) -> PreparedMutation | GuardOutcome:
        if grant.scope != "launch":
            return self.guard(request, "denied", "claim_scope_not_granted")
        work = await self.works.get(request.work_id)
        if work is None:
            return self.guard(request, "denied", "work_not_bound")
        if request.observed_revision != canonical_revision(work.work_id, work.row_version):
            return self.guard(request, "stale", "source_revision_changed")
        return PreparedMutation(
            intent={"request": request.model_dump(mode="json"),
                    "qualification": qualification},
            send=lambda: self._send(principal, request, grant.id, qualification),
        )

    async def _send(
        self, principal: PrincipalContext, request: PriorityClaimWrite,
        grant_id: UUID, qualification: str,
    ) -> GuardOutcome:
        try:
            claim = await self.repository.record(self._new(request, grant_id))
        except (LookupError, ValueError):
            return self.guard(request, "denied", "invalid_claim_state")
        return self._applied(request, principal, grant_id, qualification, claim)

    async def _reconcile(
        self, grants: GrantState, principal: PrincipalContext,
        request: PriorityClaimWrite, record: EffectRecord,
    ) -> GuardOutcome:
        qualification = record.intent.get("qualification")
        if not isinstance(qualification, str) or not qualification:
            raise ValueError("durable claim intent invalid")
        expected = self._new(request, record.grant_id)
        prior = await self.repository.provenance(expected.claim_id, limit=1)
        if not prior:
            return self.guard(request, "unknown", "effect_readback_unconfirmed", possible=True)
        if not self._same(prior[0], expected):
            return self.guard(request, "denied", "claim_identity_conflict")
        outcome = self._applied(request, principal, record.grant_id, qualification, prior[0])
        await grants.finish(outcome)
        return outcome
