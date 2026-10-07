from switchstand.stacked_delivery import proportional_qualification_is_sufficient
from switchstand.wakeful_host_qualification import (
    HostIdentity,
    JourneyEvidence,
    qualify_host_journey,
)

IDENTITY = HostIdentity("a" * 40, "b" * 64, "c" * 64, "d" * 64)


def test_complete_journey_passes_same_predicate_dimension() -> None:
    evidence = JourneyEvidence(
        True, True, True, True, True, True, True, True, 0, 0, 0,
        observed_identity=IDENTITY,
        observed_nonce="fresh-nonce-0123456789",
        session_id="thread-1",
    )
    result = qualify_host_journey(IDENTITY, "fresh-nonce-0123456789", evidence)
    assert (result.verdict, result.reason) == ("PASS", "complete-attributed-journey")
    assert proportional_qualification_is_sufficient(result.proportional_evidence())


def test_stable_zero_turn_no_rollout_negative_control_catches_escape() -> None:
    evidence = JourneyEvidence(
        True, True, True, False, True, None, None, None, None, None, None,
        lower_fidelity_checks_green=True,
        resume_failure="zero-turn-no-rollout",
        observed_identity=IDENTITY,
        session_id="thread-1",
    )
    assert evidence.lower_fidelity_checks_green
    result = qualify_host_journey(IDENTITY, "negative-nonce-012345", evidence)
    assert (result.verdict, result.reason) == ("FAIL", "zero-turn-no-rollout")
    assert not proportional_qualification_is_sufficient(result.proportional_evidence())


def test_missing_evidence_and_residue_fail_closed() -> None:
    incomplete = JourneyEvidence(
        True, True, True, True, True, True, True, True, 0, None, 0,
        observed_identity=IDENTITY, observed_nonce="incomplete-nonce-0123", session_id="thread-1",
    )
    assert qualify_host_journey(IDENTITY, "incomplete-nonce-0123", incomplete).verdict == "UNKNOWN"
    residue = JourneyEvidence(
        True, True, True, True, True, True, True, True, 0, 1, 0,
        observed_identity=IDENTITY, observed_nonce="residue-nonce-012345", session_id="thread-1",
    )
    assert qualify_host_journey(IDENTITY, "residue-nonce-012345", residue).verdict == "FAIL"
    invalid = HostIdentity("bad", "b" * 64, "c" * 64, "d" * 64)
    assert qualify_host_journey(invalid, "identity-nonce-0123", residue).verdict == "UNKNOWN"


def test_evidence_cannot_be_relabelled_to_another_candidate_or_nonce() -> None:
    evidence = JourneyEvidence(
        True, True, True, True, True, True, True, True, 0, 0, 0,
        observed_identity=IDENTITY,
        observed_nonce="bound-nonce-012345",
        session_id="thread-1",
    )
    other = HostIdentity("e" * 40, "b" * 64, "c" * 64, "d" * 64)
    assert qualify_host_journey(other, "bound-nonce-012345", evidence).reason == (
        "journey-binding-mismatch"
    )
    assert qualify_host_journey(IDENTITY, "different-nonce-0123", evidence).reason == (
        "journey-binding-mismatch"
    )

def test_malformed_identity_and_nonce_are_unknown() -> None:
    malformed = (
        HostIdentity("bad", "b" * 64, "c" * 64, "d" * 64),
        HostIdentity("a" * 40, "bad", "c" * 64, "d" * 64),
        HostIdentity("a" * 40, "b" * 64, "c" * 64, "d" * 64, "future-v2"),
    )
    for identity, nonce in [
        *((identity, "valid-nonce-012345") for identity in malformed),
        (IDENTITY, "short"),
    ]:
        evidence = JourneyEvidence(
            True, True, True, True, True, True, True, True, 0, 0, 0,
            observed_identity=identity, observed_nonce=nonce, session_id="thread-1",
        )
        assert qualify_host_journey(identity, nonce, evidence).reason == "identity-unavailable"
