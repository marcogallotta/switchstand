"""Durable, default-off activation acceptance and adoption continuity."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from .contracts import ClosedModel
from .grants import PrincipalContext, WorkGrant
from .product_currentness import ProductCurrentness

NAMESPACE = UUID("28d32786-b92b-5624-9afd-dcf5bf57af51")
Transition = Literal[
    "ACTIVATED",
    "ACKNOWLEDGED",
    "BLOCK",
    "CLEAR_BLOCKER",
    "ACCEPTANCE_PASS",
    "ACCEPTANCE_FAIL",
    "ACCEPTANCE_UNKNOWN",
    "ADOPTION_ADOPTED",
    "FINALIZE_DELIVERY",
    "DEFER",
    "RETIRE",
]
ProofKind = Literal["ACCEPTANCE", "ADOPTION", "CLEARING"]
TERMINAL = frozenset({"DELIVERED", "DEFERRED", "RETIRED"})


class ActivationContract(ClosedModel):
    """Server-owned immutable product contract; never accepted from an MCP caller."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    product_work_id: UUID
    outcome_key: str = Field(min_length=1, max_length=120)
    target_revision: str = Field(min_length=1, max_length=512)
    target_phase: str = Field(min_length=1, max_length=120)
    return_owner_work_id: UUID
    acceptance_contract_id: str = Field(min_length=1, max_length=240)
    contract_revision: str = Field(min_length=1, max_length=240)
    acceptance_verifier_work_id: UUID | None = None
    adoption_requirement: Literal["NOT_REQUIRED", "REQUIRED"]
    adoption_actor_work_id: UUID | None = None
    lifecycle_authority_work_id: UUID

    @model_validator(mode="after")
    def adoption_actor_matches_requirement(self) -> Self:
        if (self.adoption_requirement == "REQUIRED") != (self.adoption_actor_work_id is not None):
            raise ValueError("required adoption needs one bound actor")
        return self

    @property
    def obligation_id(self) -> UUID:
        identity = json.dumps(
            [str(self.product_work_id), self.outcome_key, self.target_revision, self.target_phase],
            separators=(",", ":"),
        )
        return uuid5(NAMESPACE, identity)

    def stored(self) -> dict[str, object]:
        value = self.model_dump(mode="json")
        return {"obligation_id": str(self.obligation_id), **value}


class TransitionIntent(ClosedModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID
    obligation_id: UUID
    observed_revision: str = Field(min_length=1, max_length=64)
    transition: Transition
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=16)
    blocker_ref: str | None = Field(default=None, min_length=1, max_length=1000)
    clearing_event_ref: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def bounded_unique_evidence(self) -> Self:
        if len(set(self.evidence_refs)) != len(self.evidence_refs) or any(
            not value or len(value) > 1000 for value in self.evidence_refs
        ):
            raise ValueError("evidence refs must be unique bounded strings")
        return self


class TechnicalBasis(ClosedModel):
    """Server-owned binding of product currentness to the obligation target."""

    target_revision: str = Field(min_length=1, max_length=512)
    target_phase: str = Field(min_length=1, max_length=120)
    currentness: Literal["CURRENT", "STALE"]
    result: ProductCurrentness


class RuntimeBinding(ClosedModel):
    """Server-resolved current runtime assignment for one transition actor."""

    actor_work_id: UUID
    binding_token: str = Field(min_length=1, max_length=512)
    currentness: Literal["CURRENT", "STALE", "UNKNOWN"]


class TransitionProof(ClosedModel):
    """Server-resolved proof bound to the exact installed activation contract."""

    kind: ProofKind
    obligation_id: UUID
    product_work_id: UUID
    target_revision: str = Field(min_length=1, max_length=512)
    target_phase: str = Field(min_length=1, max_length=120)
    acceptance_contract_id: str = Field(min_length=1, max_length=240)
    contract_revision: str = Field(min_length=1, max_length=240)
    currentness: Literal["CURRENT", "STALE", "UNKNOWN"]
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=16)
    clearing_event_ref: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def bounded_unique_evidence(self) -> Self:
        if len(set(self.evidence_refs)) != len(self.evidence_refs) or any(
            not value or len(value) > 1000 for value in self.evidence_refs
        ):
            raise ValueError("evidence refs must be unique bounded strings")
        if (self.kind == "CLEARING") != (self.clearing_event_ref is not None):
            raise ValueError("only clearing proof carries a clearing event")
        return self


class Obligation(ClosedModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    binding: dict[str, object]
    state: Literal[
        "WAITING_ACTIVATION",
        "VERIFY_NOW",
        "VERIFYING",
        "BLOCKED",
        "ACCEPTED",
        "DELIVERED",
        "DEFERRED",
        "RETIRED",
    ]
    acceptance: Literal["NOT_RUN", "PASS", "FAIL", "UNKNOWN"]
    adoption: Literal["NOT_REQUIRED", "PENDING", "ADOPTED", "REJECTED", "UNKNOWN"]
    blocker_ref: str | None = None
    blocker_phase: str | None = None
    clearing_event_ref: str | None = None
    acceptance_proof: tuple[str, ...] = ()
    adoption_proof: tuple[str, ...] = ()
    actor_ref: str
    runtime_binding_token: str
    technical_basis_ref: str | None = None
    generation: int = Field(ge=1)
    predecessor: str | None = None
    operation_id: UUID
    intent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def coherent(self) -> Self:
        blockers = (self.blocker_ref, self.blocker_phase, self.clearing_event_ref)
        if (self.state == "BLOCKED") != all(value is not None for value in blockers):
            raise ValueError("only BLOCKED carries complete blocker evidence")
        if self.state in {"ACCEPTED", "DELIVERED"} and self.acceptance != "PASS":
            raise ValueError("accepted states require acceptance PASS")
        if self.state == "DELIVERED" and self.adoption not in {"ADOPTED", "NOT_REQUIRED"}:
            raise ValueError("delivery requires completed adoption")
        if (self.generation == 1) != (self.predecessor is None):
            raise ValueError("generation does not match predecessor")
        return self


class ContinuityResult(ClosedModel):
    status: Literal[
        "APPLIED", "REPLAYED", "CURRENT", "STALE", "CONFLICT", "DENIED", "MISSING", "UNKNOWN"
    ]
    obligation: Obligation | None = None
    reason: str | None = None


class CapabilityPreflight(ClosedModel):
    capability: Literal["activation_continuity"] = "activation_continuity"
    surface: Literal["ORDINARY_WORKSPACE", "MANAGED_LAUNCH"]
    status: Literal["READY", "MISSING_CAPABILITY", "UNKNOWN"]
    reasons: tuple[str, ...] = ()
    tool_exposed: bool = False
    operation_granted: Literal["TRUE", "FALSE", "UNKNOWN"] = "FALSE"
    actor_binding: Literal["CURRENT", "STALE", "UNKNOWN", "NOT_APPLICABLE"] = "NOT_APPLICABLE"
    contract: Literal["INSTALLED", "MISSING", "UNKNOWN", "NOT_APPLICABLE"] = "NOT_APPLICABLE"
    technical_proof: Literal["READY", "MISSING", "STALE", "UNKNOWN", "NOT_APPLICABLE"] = "NOT_APPLICABLE"
    acceptance_proof_route: Literal["READY", "MISSING", "UNKNOWN", "NOT_APPLICABLE"] = "NOT_APPLICABLE"

def _digest(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def seal_obligation(value: Obligation) -> Obligation:
    payload = value.model_dump(mode="json", exclude={"digest"})
    return value.model_copy(update={"digest": _digest(payload)})


def actor_ref(
    principal: PrincipalContext, grant: WorkGrant, runtime: RuntimeBinding,
) -> str:
    return _digest({
        "principal": principal.key,
        "grant": str(grant.id),
        "version": grant.version,
        "runtime": runtime.model_dump(mode="json"),
    })


def intent_digest(intent: TransitionIntent, actor: str) -> str:
    return _digest({"intent": intent.model_dump(mode="json"), "actor_ref": actor})


def _technical_true(contract: ActivationContract, basis: TechnicalBasis | None) -> bool:
    return basis is not None and (
        basis.target_revision == contract.target_revision
        and basis.target_phase == contract.target_phase
        and basis.currentness == "CURRENT"
        and basis.result.product_work_id == contract.product_work_id
        and basis.result.status == "ok"
        and basis.result.current == "TRUE"
        and not basis.result.blockers
    )


def _proof_matches(
    contract: ActivationContract,
    intent: TransitionIntent,
    proof: TransitionProof | None,
    kind: ProofKind,
) -> bool:
    return proof is not None and (
        proof.kind == kind
        and proof.currentness == "CURRENT"
        and proof.obligation_id == contract.obligation_id
        and proof.product_work_id == contract.product_work_id
        and proof.target_revision == contract.target_revision
        and proof.target_phase == contract.target_phase
        and proof.acceptance_contract_id == contract.acceptance_contract_id
        and proof.contract_revision == contract.contract_revision
        and proof.clearing_event_ref == intent.clearing_event_ref
    )


def authorized(contract: ActivationContract, actor_work_id: UUID, transition: Transition) -> bool:
    if transition == "ACTIVATED":
        return actor_work_id == contract.product_work_id
    if transition == "ACKNOWLEDGED":
        return actor_work_id == contract.return_owner_work_id
    if transition.startswith("ACCEPTANCE_"):
        return actor_work_id in {
            contract.return_owner_work_id,
            contract.acceptance_verifier_work_id,
        }
    if transition == "ADOPTION_ADOPTED":
        return actor_work_id == contract.adoption_actor_work_id
    if transition in {"DEFER", "RETIRE"}:
        return actor_work_id == contract.lifecycle_authority_work_id
    return actor_work_id in {
        contract.return_owner_work_id,
        contract.acceptance_verifier_work_id,
        contract.product_work_id,
    }


def next_action(value: Obligation) -> str | None:
    if value.state == "VERIFYING":
        return "VERIFY_NOW" if value.acceptance == "NOT_RUN" else "REMEDIATE_REVERIFY"
    return {
        "WAITING_ACTIVATION": "WAIT_ACTIVATION_CONDITION",
        "VERIFY_NOW": "VERIFY_NOW",
        "BLOCKED": "WAIT_CLEARING_EVENT",
        "ACCEPTED": "COMPLETE_ADOPTION" if value.adoption == "PENDING" else "FINALIZE_DELIVERY",
    }.get(value.state)


def open_obligation(
    contract: ActivationContract,
    intent: TransitionIntent,
    actor: str,
    runtime_binding_token: str,
    technical: TechnicalBasis | None,
) -> ContinuityResult:
    if intent.transition != "ACTIVATED" or intent.observed_revision != "MISSING":
        return ContinuityResult(status="MISSING", reason="obligation_missing")
    if not _technical_true(contract, technical):
        return ContinuityResult(status="DENIED", reason="activation_unproved")
    assert technical is not None
    value = Obligation(
        binding=contract.stored(),
        state="VERIFY_NOW",
        acceptance="NOT_RUN",
        adoption="NOT_REQUIRED" if contract.adoption_requirement == "NOT_REQUIRED" else "PENDING",
        actor_ref=actor,
        runtime_binding_token=runtime_binding_token,
        technical_basis_ref=str(technical.result.reconciliation_id),
        generation=1,
        operation_id=intent.operation_id,
        intent_digest=intent_digest(intent, actor),
        digest="0" * 64,
    )
    return ContinuityResult(status="APPLIED", obligation=seal_obligation(value))


def advance_obligation(
    contract: ActivationContract,
    current: Obligation,
    intent: TransitionIntent,
    actor_ref: str,
    runtime_binding_token: str,
    technical: TechnicalBasis | None,
    proof: TransitionProof | None = None,
) -> ContinuityResult:
    if current.operation_id == intent.operation_id:
        status = (
            "REPLAYED" if current.intent_digest == intent_digest(intent, actor_ref) else "CONFLICT"
        )
        return ContinuityResult(
            status=status,
            obligation=current,
            reason=None if status == "REPLAYED" else "operation_identity_conflict",
        )
    if current.digest != intent.observed_revision:
        return ContinuityResult(status="STALE", obligation=current, reason="revision_changed")
    if current.state in TERMINAL:
        return ContinuityResult(status="DENIED", obligation=current, reason="obligation_terminal")
    state, changes = current.state, {}
    transition = intent.transition
    if state == "BLOCKED" and transition not in {"CLEAR_BLOCKER", "DEFER", "RETIRE"}:
        return ContinuityResult(
            status="DENIED", obligation=current, reason="transition_not_applicable"
        )
    if transition == "ACTIVATED" and state == "WAITING_ACTIVATION":
        if not _technical_true(contract, technical):
            return ContinuityResult(
                status="DENIED", obligation=current, reason="activation_unproved"
            )
        assert technical is not None
        changes = {
            "state": "VERIFY_NOW",
            "technical_basis_ref": str(technical.result.reconciliation_id),
        }
    elif transition == "ACKNOWLEDGED" and state == "VERIFY_NOW":
        changes = {"state": "VERIFYING"}
    elif transition == "BLOCK" and intent.blocker_ref and intent.clearing_event_ref:
        changes = {
            "state": "BLOCKED",
            "blocker_ref": intent.blocker_ref,
            "blocker_phase": state,
            "clearing_event_ref": intent.clearing_event_ref,
        }
    elif transition == "CLEAR_BLOCKER" and state == "BLOCKED":
        if (
            intent.clearing_event_ref != current.clearing_event_ref
            or not _proof_matches(contract, intent, proof, "CLEARING")
        ):
            return ContinuityResult(
                status="DENIED", obligation=current, reason="clearing_proof_invalid"
            )
        changes = {
            "state": current.blocker_phase,
            "blocker_ref": None,
            "blocker_phase": None,
            "clearing_event_ref": None,
        }
    elif transition.startswith("ACCEPTANCE_") and state in {"VERIFY_NOW", "VERIFYING", "ACCEPTED"}:
        result = transition.removeprefix("ACCEPTANCE_")
        if not _proof_matches(contract, intent, proof, "ACCEPTANCE"):
            return ContinuityResult(status="DENIED", obligation=current, reason="proof_invalid")
        assert proof is not None
        changes = {
            "acceptance": result,
            "acceptance_proof": proof.evidence_refs,
            "state": "ACCEPTED" if result == "PASS" else "VERIFYING",
        }
    elif (
        transition == "ADOPTION_ADOPTED"
        and _proof_matches(contract, intent, proof, "ADOPTION")
    ):
        assert proof is not None
        changes = {"adoption": "ADOPTED", "adoption_proof": proof.evidence_refs}
    elif transition == "FINALIZE_DELIVERY" and state == "ACCEPTED":
        changes = {}
    elif transition in {"DEFER", "RETIRE"}:
        changes = {
            "state": "DEFERRED" if transition == "DEFER" else "RETIRED",
            "blocker_ref": None,
            "blocker_phase": None,
            "clearing_event_ref": None,
        }
    else:
        return ContinuityResult(
            status="DENIED", obligation=current, reason="transition_not_applicable"
        )
    candidate = current.model_copy(
        update={
            **changes,
            "actor_ref": actor_ref,
            "runtime_binding_token": runtime_binding_token,
            "generation": current.generation + 1,
            "predecessor": current.digest,
            "operation_id": intent.operation_id,
            "intent_digest": intent_digest(intent, actor_ref),
            "digest": "0" * 64,
        }
    )
    if candidate.acceptance == "PASS" and candidate.adoption in {"ADOPTED", "NOT_REQUIRED"}:
        if _technical_true(contract, technical):
            assert technical is not None
            candidate = candidate.model_copy(
                update={
                    "state": "DELIVERED",
                    "technical_basis_ref": str(technical.result.reconciliation_id),
                }
            )
        elif candidate.state == "DELIVERED":
            candidate = candidate.model_copy(update={"state": "ACCEPTED"})
    return ContinuityResult(status="APPLIED", obligation=seal_obligation(candidate))
