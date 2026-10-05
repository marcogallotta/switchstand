from uuid import UUID, uuid4

from switchstand.activation_continuity import (
    ActivationContract,
    TechnicalBasis,
    TransitionIntent,
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


def test_not_required_pass_delivers_only_on_exact_current_technical_basis():
    bound = contract()
    opened = open_obligation(
        bound, intent(bound, "ACTIVATED", "MISSING"), "actor", technical(bound)
    )
    bad = technical(bound).model_copy(update={"target_revision": "git:other"})
    accepted = advance_obligation(
        bound,
        opened.obligation,
        intent(bound, "ACCEPTANCE_PASS", opened.obligation.digest, evidence_refs=("proof",)),
        "verifier",
        bad,
    )
    assert (accepted.obligation.state, accepted.obligation.acceptance) == ("ACCEPTED", "PASS")
    final = advance_obligation(
        bound,
        accepted.obligation,
        intent(bound, "FINALIZE_DELIVERY", accepted.obligation.digest),
        "owner",
        technical(bound),
    )
    assert final.obligation.state == "DELIVERED"


def test_ack_block_and_adoption_never_imply_acceptance_or_delivery():
    bound = contract("REQUIRED")
    opened = open_obligation(
        bound, intent(bound, "ACTIVATED", "MISSING"), "product", technical(bound)
    )
    acknowledged = advance_obligation(
        bound,
        opened.obligation,
        intent(bound, "ACKNOWLEDGED", opened.obligation.digest),
        "owner",
        None,
    )
    adopted = advance_obligation(
        bound,
        acknowledged.obligation,
        intent(
            bound, "ADOPTION_ADOPTED", acknowledged.obligation.digest, evidence_refs=("adoption",)
        ),
        "adopter",
        technical(bound),
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
        None,
    )
    wrong = advance_obligation(
        bound,
        blocked.obligation,
        intent(bound, "CLEAR_BLOCKER", blocked.obligation.digest, clearing_event_ref="wrong"),
        "owner",
        None,
    )
    assert (adopted.obligation.state, adopted.obligation.acceptance) == ("VERIFYING", "NOT_RUN")
    assert (wrong.status, wrong.reason) == ("DENIED", "clearing_event_mismatch")


def test_exact_replay_converges_and_changed_operation_conflicts():
    bound = contract()
    operation = intent(bound, "ACTIVATED", "MISSING")
    opened = open_obligation(bound, operation, "product", technical(bound))
    replay = advance_obligation(bound, opened.obligation, operation, "product", technical(bound))
    changed = advance_obligation(
        bound,
        opened.obligation,
        operation.model_copy(update={"transition": "RETIRE"}),
        "product",
        None,
    )
    assert replay.status == "REPLAYED"
    assert (changed.status, changed.reason) == ("CONFLICT", "operation_identity_conflict")
