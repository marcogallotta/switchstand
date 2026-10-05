import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.activation_continuity import (
    ActivationContract,
    RuntimeBinding,
    TechnicalBasis,
    TransitionIntent,
    TransitionProof,
)
from switchstand.activation_continuity_store import ActivationContinuity
from switchstand.contracts import LaunchAuthority
from switchstand.grants import PrincipalContext, WorkGrant
from switchstand.product_currentness import ProductCurrentness

PRODUCT = UUID("20000000-0000-4000-8000-000000000001")
OWNER = UUID("20000000-0000-4000-8000-000000000002")
VERIFIER = UUID("20000000-0000-4000-8000-000000000003")
ADOPTER = UUID("20000000-0000-4000-8000-000000000004")
LIFECYCLE = UUID("20000000-0000-4000-8000-000000000005")


def contract(adoption: str = "REQUIRED") -> ActivationContract:
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
        adoption_actor_work_id=ADOPTER if adoption == "REQUIRED" else None,
        lifecycle_authority_work_id=LIFECYCLE,
    )


def principal(name: str) -> PrincipalContext:
    return PrincipalContext(issuer="fixture", subject=name, client_id="codex", assurance="test")


def grant(actor: UUID, who: PrincipalContext) -> WorkGrant:
    return WorkGrant(
        id=uuid4(),
        version=1,
        principal=who,
        authority=LaunchAuthority(active_work_id=actor),
        operations=frozenset({"work_get"}),
        issuer="fixture",
        provenance="fixture",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def technical(bound: ActivationContract, result: str = "TRUE") -> TechnicalBasis:
    currentness = ProductCurrentness(
        status="ok" if result in {"TRUE", "FALSE"} else "unknown",
        product_work_id=PRODUCT,
        current=result,
        contract_revision="stateful-v1",
        reconciliation_id=uuid4(),
        basis_id="basis",
        conditions=(),
        blockers=() if result == "TRUE" else ("functional_proof",),
        reason=None if result in {"TRUE", "FALSE"} else "unproved",
    )
    return TechnicalBasis(
        target_revision=bound.target_revision,
        target_phase=bound.target_phase,
        currentness="CURRENT",
        result=currentness,
    )


def intent(bound: ActivationContract, transition: str, observed: str, **values) -> TransitionIntent:
    return TransitionIntent(
        operation_id=uuid4(),
        obligation_id=bound.obligation_id,
        observed_revision=observed,
        transition=transition,
        **values,
    )


def proof(bound: ActivationContract, operation: TransitionIntent) -> TransitionProof | None:
    kind = (
        "ACCEPTANCE" if operation.transition.startswith("ACCEPTANCE_")
        else "ADOPTION" if operation.transition == "ADOPTION_ADOPTED"
        else "CLEARING" if operation.transition == "CLEAR_BLOCKER"
        else None
    )
    if kind is None:
        return None
    return TransitionProof(
        kind=kind,
        obligation_id=bound.obligation_id,
        product_work_id=bound.product_work_id,
        target_revision=bound.target_revision,
        target_phase=bound.target_phase,
        acceptance_contract_id=bound.acceptance_contract_id,
        contract_revision=bound.contract_revision,
        currentness="CURRENT",
        evidence_refs=operation.evidence_refs or ("clearing-proof",),
        clearing_event_ref=(
            operation.clearing_event_ref if kind == "CLEARING" else None
        ),
    )


@pytest.fixture
async def subject(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL") or pytest.skip("TEST_DATABASE_URL is required")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS activation_obligation_revisions"))
        await connection.execute(
            text("""
            CREATE TABLE activation_obligation_revisions (
              obligation_id uuid NOT NULL, operation_id uuid NOT NULL UNIQUE,
              generation bigint NOT NULL, record jsonb NOT NULL,
              PRIMARY KEY (obligation_id, generation))
        """)
        )
    bound = contract()
    yield ActivationContinuity(engine, {bound.obligation_id: bound}), bound
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS activation_obligation_revisions"))
    await engine.dispose()


async def apply(subject, bound, actor, transition, observed, technical_basis=None, **values):
    who = principal(str(actor))
    operation = intent(bound, transition, observed, **values)
    return await subject.transition(
        who,
        grant(actor, who),
        RuntimeBinding(
            actor_work_id=actor,
            binding_token=f"runtime/{actor}",
            currentness="CURRENT",
        ),
        operation,
        technical_basis,
        proof(bound, operation),
    )


async def test_server_owned_creator_and_acceptance_adoption_guards(subject):
    state, bound = subject
    missing = await state.get(bound.obligation_id)
    assert missing.status == "MISSING"
    denied = await apply(state, bound, OWNER, "ACTIVATED", "MISSING", technical(bound))
    assert denied.status == "DENIED"
    opened = await apply(state, bound, PRODUCT, "ACTIVATED", "MISSING", technical(bound))
    projected = await state.for_owner(OWNER)
    assert len(projected) == 1 and projected[0].digest == opened.obligation.digest
    assert (opened.status, opened.obligation.state) == ("APPLIED", "VERIFY_NOW")
    adopted = await apply(
        state,
        bound,
        ADOPTER,
        "ADOPTION_ADOPTED",
        opened.obligation.digest,
        technical(bound),
        evidence_refs=("adoption-proof",),
    )
    assert (adopted.obligation.state, adopted.obligation.adoption) == ("VERIFY_NOW", "ADOPTED")
    accepted = await apply(
        state,
        bound,
        VERIFIER,
        "ACCEPTANCE_PASS",
        adopted.obligation.digest,
        technical(bound),
        evidence_refs=("acceptance-proof",),
    )
    assert (accepted.obligation.state, accepted.obligation.acceptance) == ("DELIVERED", "PASS")


async def test_fail_closed_target_currentness_blocker_and_lifecycle_authority(subject):
    state, bound = subject
    wrong_target = technical(bound).model_copy(update={"target_revision": "git:wrong"})
    denied = await apply(state, bound, PRODUCT, "ACTIVATED", "MISSING", wrong_target)
    assert (denied.status, denied.reason) == ("DENIED", "activation_unproved")
    opened = await apply(state, bound, PRODUCT, "ACTIVATED", "MISSING", technical(bound))
    blocked = await apply(
        state,
        bound,
        OWNER,
        "BLOCK",
        opened.obligation.digest,
        blocker_ref="incident",
        clearing_event_ref="fixed",
    )
    wrong = await apply(
        state,
        bound,
        OWNER,
        "CLEAR_BLOCKER",
        blocked.obligation.digest,
        clearing_event_ref="other",
    )
    assert (wrong.status, wrong.reason) == ("DENIED", "clearing_proof_invalid")
    denied = await apply(state, bound, OWNER, "RETIRE", blocked.obligation.digest)
    retired = await apply(state, bound, LIFECYCLE, "RETIRE", blocked.obligation.digest)
    assert denied.status == "DENIED" and retired.obligation.state == "RETIRED"


async def test_concurrent_replay_and_corruption_fail_closed(subject):
    state, bound = subject
    who, operation = principal("product"), uuid4()
    first_intent = TransitionIntent(
        operation_id=operation,
        obligation_id=bound.obligation_id,
        observed_revision="MISSING",
        transition="ACTIVATED",
    )
    current_grant = grant(PRODUCT, who)
    first, replay = await asyncio.gather(
        *(state.transition(
            who, current_grant,
            RuntimeBinding(
                actor_work_id=PRODUCT, binding_token="runtime/product",
                currentness="CURRENT",
            ),
            first_intent, technical(bound),
        ) for _ in range(2))
    )
    assert {first.status, replay.status} == {"APPLIED", "REPLAYED"}
    changed = await state.transition(
        who, grant(PRODUCT, who),
        RuntimeBinding(
            actor_work_id=PRODUCT, binding_token="runtime/product",
            currentness="CURRENT",
        ),
        first_intent.model_copy(update={"transition": "RETIRE"}),
    )
    assert changed.status in {"CONFLICT", "DENIED"}
    async with state.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE activation_obligation_revisions SET record=jsonb_set(record,'{state}','\"DELIVERED\"')"
            )
        )
    corrupt = await state.get(bound.obligation_id)
    assert (corrupt.status, corrupt.reason) == ("UNKNOWN", "corrupt_revision_chain")
    assert await state.for_owner(OWNER) == "UNKNOWN"


async def test_runtime_replacement_and_installed_authority_binding_are_fenced(subject):
    state, bound = subject
    opened = await apply(
        state, bound, PRODUCT, "ACTIVATED", "MISSING", technical(bound)
    )
    who = principal("owner")
    # Use one principal object for the grant and call so only runtime currentness is at issue.
    selected = grant(OWNER, who)
    pickup = intent(bound, "ACKNOWLEDGED", opened.obligation.digest)
    stale = await state.transition(
        who, selected,
        RuntimeBinding(
            actor_work_id=OWNER, binding_token="runtime/old", currentness="STALE"
        ),
        pickup,
    )
    assert (stale.status, stale.reason) == ("STALE", "runtime_binding_not_current")
    replacement = await state.transition(
        who, selected,
        RuntimeBinding(
            actor_work_id=OWNER, binding_token="runtime/replacement",
            currentness="CURRENT",
        ),
        pickup,
    )
    assert replacement.status == "APPLIED"
    assert replacement.obligation.runtime_binding_token == "runtime/replacement"

    state.contracts[bound.obligation_id] = bound.model_copy(
        update={"lifecycle_authority_work_id": OWNER}
    )
    conflict = await state.transition(
        who, selected,
        RuntimeBinding(
            actor_work_id=OWNER, binding_token="runtime/replacement",
            currentness="CURRENT",
        ),
        intent(
            bound, "BLOCK", replacement.obligation.digest,
            blocker_ref="incident", clearing_event_ref="fixed",
        ),
    )
    assert (conflict.status, conflict.reason) == ("CONFLICT", "stored_binding_mismatch")
