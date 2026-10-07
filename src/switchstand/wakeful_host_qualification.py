"""Claim-specific Wakeful real-host qualification without product state."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

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
    observed_session_id: str | None = None


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
    observed_session_id: str | None


def observe_same_session_turn(
    thread: dict[str, Any], wake_id: str, nonce: str,
) -> SameSessionTurnObservation:
    """Distinguish queue consumption from a nonce-attributed assistant rollout."""

    session_id = thread.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        return SameSessionTurnObservation(None, None, "thread-observation-unavailable", None)
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return SameSessionTurnObservation(None, None, "thread-observation-unavailable", session_id)
    matches: list[tuple[dict[str, Any], int]] = []
    for raw_turn in cast(list[Any], turns):
        if not isinstance(raw_turn, dict):
            return SameSessionTurnObservation(None, None, "thread-observation-unavailable", session_id)
        turn = cast(dict[str, Any], raw_turn)
        items = turn.get("items")
        if not isinstance(items, list):
            return SameSessionTurnObservation(None, None, "thread-observation-unavailable", session_id)
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
        return SameSessionTurnObservation(None, None, "wake-turn-ambiguous", session_id)
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
        return SameSessionTurnObservation(True, nonce, None, session_id)
    if turn.get("status") in {"completed", "failed", "cancelled"}:
        failure = "nonce-not-observed" if assistant_text else "zero-turn-no-rollout"
        return SameSessionTurnObservation(False, None, failure, session_id)
    return SameSessionTurnObservation(None, None, "turn-incomplete", session_id)


def _identity_and_nonce_are_valid(identity: HostIdentity, nonce: str) -> bool:
    return (
        _SHA.fullmatch(identity.candidate_sha) is not None
        and all(_DIGEST.fullmatch(value) is not None for value in (
            identity.launcher_digest, identity.profile_digest, identity.config_digest,
        ))
        and identity.qualifier_version == "wakeful-real-host-v1"
        and len(nonce) >= 16
    )

def qualify_host_journey(
    identity: HostIdentity, nonce: str, evidence: JourneyEvidence,
) -> HostQualification:
    """Fail closed and bind PASS to exact candidate plus host-control digests."""

    if not _identity_and_nonce_are_valid(identity, nonce):
        return HostQualification("UNKNOWN", "identity-unavailable", identity, nonce)
    failure = cast(Any, evidence.resume_failure)
    if failure is not None:
        failures = ("zero-turn-no-rollout", "nonce-not-observed")
        incomplete = ("thread-observation-unavailable", "wake-turn-ambiguous", "turn-incomplete")
        verdict: HostVerdict = "FAIL" if failure in failures else "UNKNOWN"
        reason = cast(str, failure) if failure in failures + incomplete else "journey-evidence-malformed"
        return HostQualification(verdict, reason, identity, nonce)
    if (
        evidence.observed_identity is None
        or evidence.observed_nonce is None
        or evidence.session_id is None
        or evidence.observed_session_id is None
    ):
        return HostQualification("UNKNOWN", "journey-binding-unavailable", identity, nonce)
    if evidence.observed_identity != identity or evidence.observed_nonce != nonce:
        return HostQualification("FAIL", "journey-binding-mismatch", identity, nonce)
    session_id = cast(Any, evidence.session_id)
    observed_session_id = cast(Any, evidence.observed_session_id)
    if (
        not isinstance(session_id, str)
        or not session_id.strip()
        or not isinstance(observed_session_id, str)
        or not observed_session_id.strip()
    ):
        return HostQualification("UNKNOWN", "session-binding-unavailable", identity, nonce)
    if observed_session_id != session_id:
        return HostQualification("FAIL", "session-binding-mismatch", identity, nonce)
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
    if any(value is not None and type(value) is not bool for value in stages):
        return HostQualification("UNKNOWN", "journey-evidence-malformed", identity, nonce)
    if any(value is False for value in stages):
        return HostQualification("FAIL", "journey-stage-failed", identity, nonce)
    residues = (
        evidence.queue_residue, evidence.wake_residue, evidence.source_result_residue,
    )
    if any(value is not None and type(value) is not int for value in residues):
        return HostQualification("UNKNOWN", "journey-evidence-malformed", identity, nonce)
    if any(value is not None and value != 0 for value in residues):
        return HostQualification("FAIL", "journey-residue", identity, nonce)
    if any(value is None for value in (*stages, *residues)):
        return HostQualification("UNKNOWN", "journey-evidence-incomplete", identity, nonce)
    return HostQualification("PASS", "complete-attributed-journey", identity, nonce)
