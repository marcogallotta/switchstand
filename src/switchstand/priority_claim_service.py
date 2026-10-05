"""Default-off priority-claim semantics over the durable claim repository."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import Field, model_validator
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .canonical_work import CanonicalWorkRepository, canonical_revision
from .contracts import ClosedModel
from .grant_state import GrantState
from .grants import GuardOutcome, PrincipalContext, PriorityClaimReceipt
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
        fingerprint = self._fingerprint(principal, request)
        possible = False
        try:
            async with grants.locked(principal.key, request.work_id) as grant:
                exact = await grants.exact(request.operation_id)
                if exact is not None:
                    if exact.principal_key != principal.key or exact.fingerprint != fingerprint:
                        return self.guard(request, "denied", "operation_identity_conflict")
                    if exact.outcome.effect != "unknown":
                        return exact.outcome
                    grant_id = exact.grant_id
                    possible, qualification = True, str(exact.intent.get("qualification", ""))
                else:
                    if grant is None or grant.principal != principal or not grant.current():
                        return self.guard(request, "denied", "no_current_grant")
                    if (grant.scope != "launch" or not grant.can_write(request.work_id)
                            or "priority_claim" not in grant.operations):
                        return self.guard(request, "denied", "claim_scope_not_granted")
                    if request.grant_version != grant.version:
                        return self.guard(request, "stale", "grant_version_changed")
                    qualification = grant.priority_claim_qualification or ""
                    if (not qualification or (principal.assurance == "test")
                            != qualification.startswith("test:")):
                        return self.guard(request, "denied", "claim_write_not_qualified")
                    grant_id = grant.id
                expected = self._new(request, grant_id)
                prior = await self.repository.provenance(expected.claim_id, limit=1)
                if prior:
                    if not self._same(prior[0], expected):
                        return self.guard(request, "denied", "claim_identity_conflict")
                    outcome = self._applied(
                        request, principal, grant_id, qualification, prior[0]
                    )
                    if exact is not None and exact.outcome.effect == "unknown":
                        await grants.finish(outcome)
                    return outcome
                if exact is None:
                    assert grant is not None
                    work = await self.works.get(request.work_id)
                    if work is None:
                        return self.guard(request, "denied", "work_not_bound")
                    if request.observed_revision != canonical_revision(
                        work.work_id, work.row_version
                    ):
                        return self.guard(request, "stale", "source_revision_changed")
                    unknown = self.guard(
                        request, "unknown", "prepared_or_unconfirmed_write", possible=True,
                    )
                    await grants.prepare(
                        {"request": request.model_dump(mode="json"),
                         "qualification": qualification}, grant, fingerprint, unknown,
                    )
                    possible = True
                try:
                    claim = await self.repository.record(expected)
                except (IntegrityError, LookupError, ValueError):
                    prior = await self.repository.provenance(expected.claim_id, limit=1)
                    if not prior or not self._same(prior[0], expected):
                        outcome = self.guard(request, "denied", "invalid_claim_state")
                        await grants.finish(outcome)
                        return outcome
                    claim = prior[0]
                outcome = self._applied(request, principal, grant_id, qualification, claim)
                await grants.finish(outcome)
                return outcome
        except (SQLAlchemyError, TypeError, KeyError):
            return self.guard(
                request, "unknown", "state_or_effect_unavailable", possible=possible,
            )
