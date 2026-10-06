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
    StalePriorityClaim,
    SubjectKind,
)

CLAIM_NAMESPACE = UUID("43e26bcc-bd6e-446c-baa7-9babe6d15412")
HUMAN_CLAIM_NAMESPACE = UUID("33741195-b41c-47a6-a782-ff56310f8433")
HUMAN_CLEAR_NAMESPACE = UUID("86a859e4-e0cf-4fe7-98db-ec334970409a")


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


class HumanPrioritySet(ClosedModel):
    api_version: Literal["1"]
    operation_id: UUID
    subject_kind: SubjectKind
    subject_id: UUID
    observed_revision: str | None = Field(default=None, min_length=1)
    relation_kind: RelationKind
    rationale: str = Field(min_length=1, max_length=300)
    relation_target_id: UUID | None = None
    band: PriorityBand | None = None
    supersedes_claim_id: UUID | None = None

    @model_validator(mode="after")
    def valid_set(self) -> Self:
        if (self.subject_kind == "WORK") != (self.observed_revision is not None):
            raise ValueError("observed_revision is required only for WORK priority")
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


class HumanPriorityClear(ClosedModel):
    api_version: Literal["1"]
    operation_id: UUID
    subject_kind: SubjectKind
    subject_id: UUID
    claim_id: UUID
    observed_revision: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def valid_clear(self) -> Self:
        if (self.subject_kind == "WORK") != (self.observed_revision is not None):
            raise ValueError("observed_revision is required only for WORK priority")
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
            subject_id=request.work_id,
            supersedes_claim_id=claim.supersedes_claim_id,
            source_observed_revision=request.observed_revision,
            qualification=qualification,
        )
        return GuardOutcome(
            status="ok", operation="priority_claim_record", work_id=request.work_id,
            operation_id=request.operation_id, reason="priority_claim_converged",
            effect="applied", next_action="Use the recorded receipt.", receipt=receipt,
        )

    @staticmethod
    def _human_fingerprint(
        principal: PrincipalContext, request: HumanPrioritySet | HumanPriorityClear,
    ) -> str:
        payload = [principal.key, request.model_dump(mode="json")]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @classmethod
    def _human_source_ref(
        cls, principal: PrincipalContext, request: HumanPrioritySet | HumanPriorityClear,
    ) -> str:
        return f"human-via-ordinary-chatgpt-v1:{cls._human_fingerprint(principal, request)}"

    @staticmethod
    def human_guard(
        request: HumanPrioritySet | HumanPriorityClear, operation: str,
        status: Literal["ok", "denied", "stale", "not_applied", "unknown"],
        reason: str, *, possible: bool = False,
    ) -> GuardOutcome:
        return GuardOutcome(
            status=status, operation=operation,
            work_id=request.subject_id if request.subject_kind == "WORK" else None,
            operation_id=request.operation_id, reason=reason,
            effect="unknown" if possible else "not_sent",
            retry="reconcile" if possible else "refresh" if status == "stale" else "none",
            next_action=(
                "Retry this exact OperationId and identical arguments to reconcile."
                if possible else "Refresh the exact subject or use a trusted issuer."
            ),
        )

    @classmethod
    def _human_set_claim(
        cls, principal: PrincipalContext, request: HumanPrioritySet,
    ) -> NewPriorityClaim:
        return NewPriorityClaim(
            claim_id=uuid5(HUMAN_CLAIM_NAMESPACE, str(request.operation_id)),
            claim_kind="HUMAN_PRIORITY", subject_kind=request.subject_kind,
            subject_id=request.subject_id, relation_kind=request.relation_kind,
            rationale=request.rationale, source_label="HUMAN",
            source_ref=cls._human_source_ref(principal, request),
            relation_target_id=request.relation_target_id, band=request.band,
            source_observed_revision=None,
            supersedes_claim_id=request.supersedes_claim_id,
        )

    @classmethod
    def _human_clear_claim(
        cls, principal: PrincipalContext, request: HumanPriorityClear,
        target: PriorityClaim,
    ) -> NewPriorityClaim:
        return NewPriorityClaim(
            claim_id=uuid5(HUMAN_CLEAR_NAMESPACE, str(request.operation_id)),
            claim_kind="HUMAN_PRIORITY", subject_kind=request.subject_kind,
            subject_id=request.subject_id, relation_kind=target.relation_kind,
            rationale="HUMAN priority cleared", source_label="HUMAN_CLEAR",
            source_ref=cls._human_source_ref(principal, request),
            relation_target_id=target.relation_target_id, band=target.band,
            source_observed_revision=None, supersedes_claim_id=request.claim_id,
        )

    @staticmethod
    def _human_applied(
        request: HumanPrioritySet | HumanPriorityClear, operation: str,
        principal: PrincipalContext, grant: WorkGrant, qualification: str,
        claim: PriorityClaim,
    ) -> GuardOutcome:
        cleared = request.claim_id if isinstance(request, HumanPriorityClear) else None
        receipt = PriorityClaimReceipt(
            operation_id=request.operation_id, principal=principal,
            grant_id=grant.id, grant_version=grant.version,
            work_id=request.subject_id if request.subject_kind == "WORK" else None,
            action="CLEAR" if cleared is not None else "SET",
            subject_kind=request.subject_kind, subject_id=request.subject_id,
            claim_id=claim.claim_id, supersedes_claim_id=claim.supersedes_claim_id,
            cleared_claim_id=cleared,
            source_observed_revision=request.observed_revision,
            qualification=qualification,
        )
        return GuardOutcome(
            status="ok", operation=operation,
            work_id=request.subject_id if request.subject_kind == "WORK" else None,
            operation_id=request.operation_id, reason="priority_claim_converged",
            effect="applied", next_action="Use the recorded receipt.", receipt=receipt,
        )

    async def _human_admission(
        self, grants: GrantState, principal: PrincipalContext,
        request: HumanPrioritySet | HumanPriorityClear, operation: str,
    ) -> tuple[WorkGrant, str] | GuardOutcome:
        grant = await grants.current(principal.key)
        if grant is None or grant.principal != principal or not grant.current():
            return self.human_guard(request, operation, "denied", "no_current_grant")
        if grant.scope != "workspace" or "priority_claim" not in grant.operations:
            return self.human_guard(request, operation, "denied", "claim_scope_not_granted")
        qualification = grant.priority_claim_qualification
        if (
            qualification is None
            or (principal.assurance == "test") != qualification.startswith("test:")
        ):
            return self.human_guard(request, operation, "denied", "claim_write_not_qualified")
        return grant, qualification

    async def _human_revision_status(
        self, request: HumanPrioritySet | HumanPriorityClear,
    ) -> Literal["CURRENT", "STALE", "MISSING"]:
        if request.subject_kind != "WORK":
            return "CURRENT"
        work = await self.works.get(request.subject_id)
        if work is None:
            return "MISSING"
        return (
            "CURRENT" if request.observed_revision == canonical_revision(
                work.work_id, work.row_version
            ) else "STALE"
        )

    async def human_set(
        self, grants: GrantState, principal: PrincipalContext, request: HumanPrioritySet,
    ) -> GuardOutcome:
        operation = "priority_claim_set"
        try:
            admission = await self._human_admission(grants, principal, request, operation)
            if isinstance(admission, GuardOutcome):
                return admission
            grant, qualification = admission
            expected = self._human_set_claim(principal, request)
            prior = await self.repository.get(expected.claim_id)
            if prior is not None:
                if not self._same(prior, expected):
                    return self.human_guard(
                        request, operation, "denied", "operation_identity_conflict"
                    )
                return self._human_applied(
                    request, operation, principal, grant, qualification, prior
                )
            revision = await self._human_revision_status(request)
            if revision == "MISSING":
                return self.human_guard(request, operation, "denied", "invalid_claim_state")
            if revision == "STALE":
                return self.human_guard(request, operation, "stale", "source_revision_changed")
            claim = await self.repository.record(
                expected, observed_revision=request.observed_revision,
            )
            return self._human_applied(
                request, operation, principal, grant, qualification, claim
            )
        except StalePriorityClaim:
            return self.human_guard(request, operation, "stale", "source_revision_changed")
        except (LookupError, ValueError):
            return self.human_guard(request, operation, "denied", "invalid_claim_state")
        except (SQLAlchemyError, TypeError, KeyError):
            return self.human_guard(
                request, operation, "unknown", "state_or_effect_unavailable", possible=True
            )

    async def human_clear(
        self, grants: GrantState, principal: PrincipalContext, request: HumanPriorityClear,
    ) -> GuardOutcome:
        operation = "priority_claim_clear"
        try:
            admission = await self._human_admission(grants, principal, request, operation)
            if isinstance(admission, GuardOutcome):
                return admission
            grant, qualification = admission
            tombstone_id = uuid5(HUMAN_CLEAR_NAMESPACE, str(request.operation_id))
            replay = await self.repository.get(tombstone_id)
            expected_source = self._human_source_ref(principal, request)
            if replay is not None:
                if not (
                    replay.claim_kind == "HUMAN_PRIORITY"
                    and replay.subject_kind == request.subject_kind
                    and replay.subject_id == request.subject_id
                    and replay.supersedes_claim_id == request.claim_id
                    and replay.source_label == "HUMAN_CLEAR"
                    and replay.source_ref == expected_source
                    and replay.state == "SUPERSEDED"
                ):
                    return self.human_guard(
                        request, operation, "denied", "operation_identity_conflict"
                    )
                return self._human_applied(
                    request, operation, principal, grant, qualification, replay
                )
            revision = await self._human_revision_status(request)
            if revision == "MISSING":
                return self.human_guard(request, operation, "denied", "invalid_claim_state")
            if revision == "STALE":
                return self.human_guard(request, operation, "stale", "source_revision_changed")
            target = await self.repository.get(request.claim_id)
            if target is None:
                return self.human_guard(
                    request, operation, "denied", "invalid_claim_state"
                )
            expected = self._human_clear_claim(principal, request, target)
            tombstone = await self.repository.clear(
                expected,
                observed_revision=request.observed_revision,
            )
            if not self._same(tombstone, expected) or tombstone.state != "SUPERSEDED":
                return self.human_guard(
                    request, operation, "denied", "operation_identity_conflict"
                )
            return self._human_applied(
                request, operation, principal, grant, qualification, tombstone
            )
        except StalePriorityClaim:
            return self.human_guard(request, operation, "stale", "source_revision_changed")
        except (LookupError, ValueError):
            return self.human_guard(request, operation, "denied", "invalid_claim_state")
        except (SQLAlchemyError, TypeError, KeyError):
            return self.human_guard(
                request, operation, "unknown", "state_or_effect_unavailable", possible=True
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
            outcome = self.guard(request, "not_applied", "claim_absence_confirmed")
            await grants.finish(outcome)
            return outcome
        if not self._same(prior[0], expected):
            return self.guard(request, "denied", "claim_identity_conflict")
        outcome = self._applied(request, principal, record.grant_id, qualification, prior[0])
        await grants.finish(outcome)
        return outcome
