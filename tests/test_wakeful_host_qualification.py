from dataclasses import replace

from switchstand.stacked_delivery import layer_qualification_is_sufficient
from switchstand.wakeful_host_qualification import HostJourney, qualify_host_journey

NONCE = "fresh-nonce-0123456789"
CONTROLS = tuple(zip(("launcher", "profile", "config", "qualifier"), ("b" * 64,) * 4))
COMPLETE = HostJourney(
    "a" * 40, CONTROLS, CONTROLS, NONCE,
    "thread-1", "thread-1", "a" * 40, NONCE,
    True, True, True, True, True, True, True, True, 0, 0, 0,
)


def test_complete_journey_passes_shared_dimension() -> None:
    result = qualify_host_journey(COMPLETE)
    assert (result.verdict, result.reason) == ("PASS", "complete-attributed-journey")
    assert layer_qualification_is_sufficient(result.evidence)


def test_zero_turn_negative_control_fails_despite_lower_fidelity_success() -> None:
    result = qualify_host_journey(replace(COMPLETE, observation_error="zero-turn-no-rollout"))
    assert (result.verdict, result.reason) == ("FAIL", "zero-turn-no-rollout")
    assert not layer_qualification_is_sufficient(result.evidence)


def test_missing_mismatched_and_residual_evidence_fail_closed() -> None:
    cases = (
        (replace(COMPLETE, wake_residue=None), "UNKNOWN"),
        (replace(COMPLETE, wake_residue=1), "FAIL"),
        (replace(COMPLETE, observed_candidate_sha="e" * 40), "FAIL"),
        (replace(COMPLETE, candidate_sha="bad"), "UNKNOWN"),
        (replace(COMPLETE, observed_controls=()), "UNKNOWN"),
        (replace(COMPLETE, observed_controls=CONTROLS[::-1]), "UNKNOWN"),
        (replace(COMPLETE, observed_controls=CONTROLS[:-1] + (("qualifier", "c" * 64),)), "FAIL"),
        (replace(COMPLETE, consumed=None), "UNKNOWN"),
    )
    assert [qualify_host_journey(value).verdict for value, _ in cases] == [e for _, e in cases]
