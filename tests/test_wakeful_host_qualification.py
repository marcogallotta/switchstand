from dataclasses import replace

from switchstand.stacked_delivery import proportional_qualification_is_sufficient
from switchstand.wakeful_host_qualification import (
    HostIdentity,
    JourneyEvidence,
    qualify_host_journey,
)

IDENTITY = HostIdentity("a" * 40, "b" * 64, "c" * 64, "d" * 64)
NONCE = "fresh-nonce-0123456789"
COMPLETE = JourneyEvidence(
    True, True, True, True, True, True, True, True, 0, 0, 0,
    observed_identity=IDENTITY, observed_nonce=NONCE,
    session_id="thread-1", observed_session_id="thread-1",
)


def test_complete_journey_passes_same_predicate_dimension() -> None:
    result = qualify_host_journey(IDENTITY, NONCE, COMPLETE)
    assert (result.verdict, result.reason) == ("PASS", "complete-attributed-journey")
    assert proportional_qualification_is_sufficient(result.proportional_evidence())


def test_stable_zero_turn_no_rollout_negative_control_catches_escape() -> None:
    evidence = replace(
        COMPLETE, remote_resume_succeeded=False, visible_wake_without_stdin=None,
        same_session_assistant_turn=None, consumed=None, queue_residue=None,
        wake_residue=None, source_result_residue=None, lower_fidelity_checks_green=True,
        resume_failure="zero-turn-no-rollout",
    )
    result = qualify_host_journey(IDENTITY, NONCE, evidence)
    assert (result.verdict, result.reason) == ("FAIL", "zero-turn-no-rollout")
    assert not proportional_qualification_is_sufficient(result.proportional_evidence())


def test_missing_evidence_and_residue_fail_closed() -> None:
    assert qualify_host_journey(
        IDENTITY, NONCE, replace(COMPLETE, wake_residue=None),
    ).verdict == "UNKNOWN"
    assert qualify_host_journey(
        IDENTITY, NONCE, replace(COMPLETE, wake_residue=1),
    ).verdict == "FAIL"
    invalid = HostIdentity("bad", "b" * 64, "c" * 64, "d" * 64)
    assert qualify_host_journey(invalid, NONCE, COMPLETE).verdict == "UNKNOWN"


def test_evidence_cannot_be_relabelled_to_another_candidate_or_nonce() -> None:
    other = HostIdentity("e" * 40, "b" * 64, "c" * 64, "d" * 64)
    assert qualify_host_journey(other, NONCE, COMPLETE).reason == "journey-binding-mismatch"
    assert qualify_host_journey(IDENTITY, "different-nonce-0123", COMPLETE).reason == (
        "journey-binding-mismatch"
    )


def test_malformed_identity_and_nonce_are_unknown() -> None:
    for identity, nonce in [(HostIdentity("bad", "b" * 64, "c" * 64, "d" * 64), NONCE),
                            (IDENTITY, "short")]:
        assert qualify_host_journey(identity, nonce, COMPLETE).reason == "identity-unavailable"


def test_malformed_runtime_evidence_is_unknown() -> None:
    malformed = (
        replace(COMPLETE, ordinary_dispatch_pilot="UNKNOWN"),  # type: ignore[arg-type]
        replace(COMPLETE, queue_residue=True),  # type: ignore[arg-type]
        replace(COMPLETE, session_id=1),  # type: ignore[arg-type]
        replace(COMPLETE, resume_failure="turn-incomplete"),
    )
    for evidence in malformed:
        assert qualify_host_journey(IDENTITY, NONCE, evidence).verdict == "UNKNOWN"


def test_observed_session_must_match_claimed_host_session() -> None:
    result = qualify_host_journey(
        IDENTITY, NONCE, replace(COMPLETE, observed_session_id="other-thread"),
    )
    assert (result.verdict, result.reason) == ("FAIL", "session-binding-mismatch")
