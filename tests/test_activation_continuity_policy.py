from uuid import UUID, uuid4

from switchstand.activation_continuity import (
    ActivationContract,
    TechnicalBasis,
    TransitionIntent,
    TransitionProof,
    advance_obligation,
    open_obligation,
)
from switchstand.product_currentness import ProductCurrentness

PRODUCT = UUID("30000000-0000-4000-8000-000000000001")
OWNER = UUID("30000000-0000-4000-8000-000000000002")
VERIFIER = UUID("30000000-0000-4000-8000-000000000003")


def contract(adoption="NOT_REQUIRED"):
    return ActivationContract(
        product_work_id=PRODUCT,
        outcome_key="release",
        target_revision="git:abc",
        target_phase="ACTIVATED",
        return_owner_work_id=OWNER,
        acceptance_contract_id="acceptance",
        contract_revision="v1",
        acceptance_verifier_work_id=VERIFIER,
        adoption_requirement=adoption,
        adoption_actor_work_id=VERIFIER if adoption == "REQUIRED" else None,
        lifecycle_authority_work_id=PRODUCT,
    )


def technical(bound, result="TRUE"):
    value = ProductCurrentness(
        status="ok" if result != "UNKNOWN" else "unknown",
        product_work_id=PRODUCT,
        current=result,
        contract_revision="v1",
        reconciliation_id=uuid4(),
        basis_id="basis",
        conditions=(),
        blockers=() if result == "TRUE" else ("functional_proof",),
        reason=None if result != "UNKNOWN" else "unproved",
    )
    return TechnicalBasis(
        target_revision=bound.target_revision,
        target_phase=bound.target_phase,
        currentness="CURRENT",
        result=value,
    )


def intent(bound, transition, observed, **values):
    return TransitionIntent(
        operation_id=uuid4(),
        obligation_id=bound.obligation_id,
        observed_revision=observed,
        transition=transition,
        **values,
    )


def proof(bound, operation, kind, *, currentness="CURRENT", **changes):
    values = {
        "kind": kind,
        "obligation_id": bound.obligation_id,
        "product_work_id": bound.product_work_id,
        "target_revision": bound.target_revision,
        "target_phase": bound.target_phase,
        "acceptance_contract_id": bound.acceptance_contract_id,
        "contract_revision": bound.contract_revision,
        "currentness": currentness,
        "evidence_refs": operation.evidence_refs or ("server-proof",),
        "clearing_event_ref": (
            operation.clearing_event_ref if kind == "CLEARING" else None
        ),
    }
    values.update(changes)
    return TransitionProof(**values)


def test_not_required_pass_delivers_only_on_exact_current_technical_basis():
    bound = contract()
    opened = open_obligation(
        bound, intent(bound, "ACTIVATED", "MISSING"), "actor", "runtime-1", technical(bound)
    )
    bad = technical(bound).model_copy(update={"target_revision": "git:other"})
    accepted_intent = intent(
        bound, "ACCEPTANCE_PASS", opened.obligation.digest, evidence_refs=("proof",)
    )
    accepted = advance_obligation(
        bound,
        opened.obligation,
        accepted_intent,
        "verifier",
        "runtime-2",
        bad,
        proof(bound, accepted_intent, "ACCEPTANCE"),
    )
    assert (accepted.obligation.state, accepted.obligation.acceptance) == ("ACCEPTED", "PASS")
    final = advance_obligation(
        bound,
        accepted.obligation,
        intent(bound, "FINALIZE_DELIVERY", accepted.obligation.digest),
        "owner",
        "runtime-3",
        technical(bound),
    )
    assert final.obligation.state == "DELIVERED"


def test_ack_block_and_adoption_never_imply_acceptance_or_delivery():
    bound = contract("REQUIRED")
    opened = open_obligation(
        bound, intent(bound, "ACTIVATED", "MISSING"), "product", "runtime-1", technical(bound)
    )
    acknowledged = advance_obligation(
        bound,
        opened.obligation,
        intent(bound, "ACKNOWLEDGED", opened.obligation.digest),
        "owner",
        "runtime-2",
        None,
    )
    adopted_intent = intent(
        bound, "ADOPTION_ADOPTED", acknowledged.obligation.digest,
        evidence_refs=("adoption",),
    )
    adopted = advance_obligation(
        bound,
        acknowledged.obligation,
        adopted_intent,
        "adopter",
        "runtime-3",
        technical(bound),
        proof(bound, adopted_intent, "ADOPTION"),
    )
    blocked = advance_obligation(
        bound,
        adopted.obligation,
        intent(
            bound,
            "BLOCK",
            adopted.obligation.digest,
            blocker_ref="incident",
            clearing_event_ref="fixed",
        ),
        "owner",
        "runtime-2",
        None,
    )
    wrong_intent = intent(
        bound, "CLEAR_BLOCKER", blocked.obligation.digest, clearing_event_ref="wrong"
    )
    wrong = advance_obligation(
        bound,
        blocked.obligation,
        wrong_intent,
        "owner",
        "runtime-2",
        None,
        proof(bound, wrong_intent, "CLEARING"),
    )
    assert (adopted.obligation.state, adopted.obligation.acceptance) == ("VERIFYING", "NOT_RUN")
    assert (wrong.status, wrong.reason) == ("DENIED", "clearing_proof_invalid")


def test_exact_replay_converges_and_changed_operation_conflicts():
    bound = contract()
    operation = intent(bound, "ACTIVATED", "MISSING")
    opened = open_obligation(bound, operation, "product", "runtime-1", technical(bound))
    replay = advance_obligation(
        bound, opened.obligation, operation, "product", "runtime-1", technical(bound)
    )
    changed = advance_obligation(
        bound,
        opened.obligation,
        operation.model_copy(update={"transition": "RETIRE"}),
        "product",
        "runtime-1",
        None,
    )
    assert replay.status == "REPLAYED"
    assert (changed.status, changed.reason) == ("CONFLICT", "operation_identity_conflict")


def test_wrong_stale_and_unresolved_proofs_cannot_advance_success():
    bound = contract("REQUIRED")
    opened = open_obligation(
        bound, intent(bound, "ACTIVATED", "MISSING"), "product", "runtime-1",
        technical(bound),
    )
    acceptance = intent(
        bound, "ACCEPTANCE_PASS", opened.obligation.digest, evidence_refs=("proof",)
    )
    wrong = proof(
        bound, acceptance, "ACCEPTANCE", target_revision="git:wrong"
    )
    stale = proof(bound, acceptance, "ACCEPTANCE", currentness="STALE")
    for invalid in (None, wrong, stale):
        denied = advance_obligation(
            bound, opened.obligation, acceptance, "verifier", "runtime-2",
            technical(bound), invalid,
        )
        assert (denied.status, denied.obligation.acceptance) == ("DENIED", "NOT_RUN")


def test_authority_bindings_are_immutable_and_digest_bound():
    bound = contract("REQUIRED")
    changed = bound.model_copy(update={"adoption_actor_work_id": UUID(int=99)})
    assert bound.stored() != changed.stored()
    opened = open_obligation(
        bound, intent(bound, "ACTIVATED", "MISSING"), "product", "runtime-token",
        technical(bound),
    )
    assert opened.obligation.runtime_binding_token == "runtime-token"


def test_adoption_and_clearing_require_current_exact_typed_proof():
    bound = contract("REQUIRED")
    opened = open_obligation(
        bound, intent(bound, "ACTIVATED", "MISSING"), "product", "runtime-1",
        technical(bound),
    )
    adoption = intent(
        bound, "ADOPTION_ADOPTED", opened.obligation.digest,
        evidence_refs=("adoption",),
    )
    wrong_adoption = proof(
        bound, adoption, "ADOPTION", contract_revision="old"
    )
    for invalid in (None, wrong_adoption):
        denied = advance_obligation(
            bound, opened.obligation, adoption, "adopter", "runtime-2",
            technical(bound), invalid,
        )
        assert (denied.status, denied.obligation.adoption) == ("DENIED", "PENDING")

    blocked_intent = intent(
        bound, "BLOCK", opened.obligation.digest,
        blocker_ref="incident", clearing_event_ref="fixed",
    )
    blocked = advance_obligation(
        bound, opened.obligation, blocked_intent, "owner", "runtime-2", None,
    )
    clearing = intent(
        bound, "CLEAR_BLOCKER", blocked.obligation.digest,
        clearing_event_ref="fixed",
    )
    stale = proof(bound, clearing, "CLEARING", currentness="STALE")
    for invalid in (None, stale):
        denied = advance_obligation(
            bound, blocked.obligation, clearing, "owner", "runtime-2", None,
            invalid,
        )
        assert (denied.status, denied.obligation.state) == ("DENIED", "BLOCKED")
