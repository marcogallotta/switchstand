"""Claim-specific Wakeful real-host qualification without product state.

The orchestration seam is deliberately injected: production qualification must
use the ordinary ``codex-dispatch`` PILOT in a persistent host PTY, while tests
can preserve the historical zero-turn/no-rollout escape as a stable control.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from .stacked_delivery import EvidenceDimension, ProportionalQualification

HostVerdict = Literal["PASS", "FAIL", "UNKNOWN"]
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class HostIdentity:
    candidate_sha: str
    launcher_digest: str
    profile_digest: str
    config_digest: str
    qualifier_version: str = "wakeful-real-host-v1"


@dataclass(frozen=True)
class JourneyEvidence:
    ordinary_dispatch_pilot: bool | None
    persistent_host_pty: bool | None
    exact_registration_binding: bool | None
    remote_resume_succeeded: bool | None
    nonce_committed: bool | None
    visible_wake_without_stdin: bool | None
    same_session_assistant_turn: bool | None
    consumed: bool | None
    queue_residue: int | None
    wake_residue: int | None
    source_result_residue: int | None
    lower_fidelity_checks_green: bool | None = None
    resume_failure: str | None = None
    observed_identity: HostIdentity | None = None
    observed_nonce: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class HostQualification:
    verdict: HostVerdict
    reason: str
    identity: HostIdentity
    nonce: str

    def proportional_evidence(self) -> ProportionalQualification:
        dimension: frozenset[EvidenceDimension] = frozenset({"WAKEFUL_REAL_HOST"})
        empty: frozenset[EvidenceDimension] = frozenset()
        return ProportionalQualification(
            self.identity.candidate_sha,
            required=dimension,
            passed=dimension if self.verdict == "PASS" else empty,
            unknown=dimension if self.verdict == "UNKNOWN" else empty,
        )


@dataclass(frozen=True)
class SameSessionTurnObservation:
    same_session_assistant_turn: bool | None
    observed_nonce: str | None
    resume_failure: str | None


def observe_same_session_turn(
    thread: dict[str, Any], wake_id: str, nonce: str,
) -> SameSessionTurnObservation:
    """Distinguish queue consumption from a nonce-attributed assistant rollout."""

    turns = thread.get("turns")
    if not isinstance(turns, list):
        return SameSessionTurnObservation(None, None, "thread-observation-unavailable")
    matches: list[tuple[dict[str, Any], int]] = []
    for raw_turn in cast(list[Any], turns):
        if not isinstance(raw_turn, dict):
            return SameSessionTurnObservation(None, None, "thread-observation-unavailable")
        turn = cast(dict[str, Any], raw_turn)
        items = turn.get("items")
        if not isinstance(items, list):
            return SameSessionTurnObservation(None, None, "thread-observation-unavailable")
        for index, raw_item in enumerate(cast(list[Any], items)):
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[str, Any], raw_item)
            if (
                item.get("type") == "userMessage"
                and item.get("clientId") == wake_id
            ):
                matches.append((turn, index))
    if len(matches) != 1:
        return SameSessionTurnObservation(None, None, "wake-turn-ambiguous")
    turn, user_index = matches[0]
    assistant_text: list[str] = []
    for raw_item in cast(list[Any], turn["items"])[user_index + 1:]:
        if not isinstance(raw_item, dict):
            continue
        item = cast(dict[str, Any], raw_item)
        text = item.get("text")
        if item.get("type") == "agentMessage" and isinstance(text, str):
            assistant_text.append(text)
    if any(nonce in text for text in assistant_text):
        return SameSessionTurnObservation(True, nonce, None)
    if turn.get("status") in {"completed", "failed", "cancelled"}:
        failure = "nonce-not-observed" if assistant_text else "zero-turn-no-rollout"
        return SameSessionTurnObservation(False, None, failure)
    return SameSessionTurnObservation(None, None, "turn-incomplete")


class JourneyDriver(Protocol):
    """Real implementations must execute each method against one host session."""

    def observed_binding(self) -> tuple[HostIdentity | None, str | None]: ...
    def launch_ordinary_pilot(self) -> tuple[bool | None, bool | None]: ...
    def bind_registration(self) -> bool | None: ...
    def resume_session(self) -> bool | None: ...
    def commit_nonce(self, nonce: str) -> bool | None: ...
    def await_visible_wake(self) -> bool | None: ...
    def await_same_session_turn(self, nonce: str) -> tuple[bool | None, str | None]: ...
    def consume_and_read_residue(self) -> tuple[bool | None, int | None, int | None, int | None]: ...
    def diagnostic(self) -> tuple[bool | None, str | None]: ...


def collect_journey(
    driver: JourneyDriver, expected_identity: HostIdentity, nonce: str,
) -> JourneyEvidence:
    """Run the ordered falsifier; a first definite failure stops the journey."""

    launched, persistent_pty = driver.launch_ordinary_pilot()
    if launched is not True or persistent_pty is not True:
        lower, failure = driver.diagnostic()
        return JourneyEvidence(
            launched, persistent_pty, None, None, None, None, None, None, None, None,
            None, lower, failure,
        )
    bound = driver.bind_registration()
    if bound is not True:
        lower, failure = driver.diagnostic()
        return JourneyEvidence(True, True, bound, None, None, None, None, None, None, None,
                               None, lower, failure)
    observed_identity, session_id = driver.observed_binding()
    if observed_identity != expected_identity or session_id is None or not session_id.strip():
        lower, failure = driver.diagnostic()
        return JourneyEvidence(
            True, True, True, None, None, None, None, None, None, None, None,
            lower, failure or "journey-binding-mismatch", observed_identity, None, session_id,
        )
    committed = driver.commit_nonce(nonce)
    if committed is not True:
        lower, failure = driver.diagnostic()
        return JourneyEvidence(True, True, True, None, committed, None, None, None, None,
                               None, None, lower, failure, observed_identity, None, session_id)
    resumed = driver.resume_session()
    if resumed is not True:
        lower, failure = driver.diagnostic()
        return JourneyEvidence(True, True, True, resumed, True, None, None, None, None, None,
                               None, lower, failure, observed_identity, None, session_id)
    wake = driver.await_visible_wake()
    if wake is not True:
        lower, failure = driver.diagnostic()
        return JourneyEvidence(True, True, True, True, True, wake, None, None, None, None,
                               None, lower, failure, observed_identity, None, session_id)
    turn, observed_nonce = driver.await_same_session_turn(nonce)
    if turn is not True:
        lower, failure = driver.diagnostic()
        return JourneyEvidence(True, True, True, True, True, True, turn, None, None, None,
                               None, lower, failure, observed_identity, observed_nonce, session_id)
    consumed, queue, wake_count, source = driver.consume_and_read_residue()
    lower, failure = driver.diagnostic()
    return JourneyEvidence(
        True, True, True, True, True, True, True, consumed, queue, wake_count, source,
        lower, failure, observed_identity, observed_nonce, session_id,
    )


def qualify_host_journey(
    identity: HostIdentity, nonce: str, evidence: JourneyEvidence,
) -> HostQualification:
    """Fail closed and bind PASS to exact candidate plus host-control digests."""

    identity_valid = (
        _SHA.fullmatch(identity.candidate_sha) is not None
        and all(_DIGEST.fullmatch(value) is not None for value in (
            identity.launcher_digest, identity.profile_digest, identity.config_digest,
        ))
        and identity.qualifier_version == "wakeful-real-host-v1"
        and len(nonce) >= 16
    )
    if not identity_valid:
        return HostQualification("UNKNOWN", "identity-unavailable", identity, nonce)
    if evidence.resume_failure == "zero-turn-no-rollout":
        return HostQualification("FAIL", "zero-turn-no-rollout", identity, nonce)
    if (
        evidence.observed_identity is None
        or evidence.observed_nonce is None
        or evidence.session_id is None
    ):
        return HostQualification("UNKNOWN", "journey-binding-unavailable", identity, nonce)
    if evidence.observed_identity != identity or evidence.observed_nonce != nonce:
        return HostQualification("FAIL", "journey-binding-mismatch", identity, nonce)
    if not evidence.session_id.strip():
        return HostQualification("UNKNOWN", "session-binding-unavailable", identity, nonce)
    stages = (
        evidence.ordinary_dispatch_pilot,
        evidence.persistent_host_pty,
        evidence.exact_registration_binding,
        evidence.remote_resume_succeeded,
        evidence.nonce_committed,
        evidence.visible_wake_without_stdin,
        evidence.same_session_assistant_turn,
        evidence.consumed,
    )
    if any(value is False for value in stages):
        return HostQualification("FAIL", "journey-stage-failed", identity, nonce)
    residues = (
        evidence.queue_residue, evidence.wake_residue, evidence.source_result_residue,
    )
    if any(value is not None and value != 0 for value in residues):
        return HostQualification("FAIL", "journey-residue", identity, nonce)
    if any(value is None for value in (*stages, *residues)):
        return HostQualification("UNKNOWN", "journey-evidence-incomplete", identity, nonce)
    return HostQualification("PASS", "complete-attributed-journey", identity, nonce)
