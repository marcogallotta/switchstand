from dataclasses import dataclass

from switchstand.stacked_delivery import proportional_qualification_is_sufficient
from switchstand.wakeful_host_qualification import (
    HostIdentity,
    JourneyEvidence,
    collect_journey,
    qualify_host_journey,
)

IDENTITY = HostIdentity("a" * 40, "b" * 64, "c" * 64, "d" * 64)


@dataclass
class FixtureDriver:
    no_rollout: bool = False

    def observed_binding(self, nonce: str):
        return IDENTITY, nonce, "thread-1"

    def launch_ordinary_pilot(self):
        return True, True

    def bind_registration(self):
        return True

    def resume_session(self):
        return not self.no_rollout

    def commit_nonce(self, nonce: str) -> bool:
        del nonce
        return True

    def await_visible_wake(self):
        return True

    def await_same_session_turn(self, nonce: str) -> bool:
        del nonce
        return True

    def consume_and_read_residue(self):
        return True, 0, 0, 0

    def diagnostic(self):
        return True, "zero-turn-no-rollout" if self.no_rollout else None


def test_complete_journey_passes_same_predicate_dimension() -> None:
    evidence = collect_journey(FixtureDriver(), "fresh-nonce-0123456789")
    result = qualify_host_journey(IDENTITY, "fresh-nonce-0123456789", evidence)
    assert (result.verdict, result.reason) == ("PASS", "complete-attributed-journey")
    assert proportional_qualification_is_sufficient(result.proportional_evidence())


def test_stable_zero_turn_no_rollout_negative_control_catches_escape() -> None:
    evidence = collect_journey(FixtureDriver(no_rollout=True), "negative-nonce-012345")
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
    evidence = collect_journey(FixtureDriver(), "bound-nonce-012345")
    other = HostIdentity("e" * 40, "b" * 64, "c" * 64, "d" * 64)
    assert qualify_host_journey(other, "bound-nonce-012345", evidence).reason == (
        "journey-binding-mismatch"
    )
    assert qualify_host_journey(IDENTITY, "different-nonce-0123", evidence).reason == (
        "journey-binding-mismatch"
    )
