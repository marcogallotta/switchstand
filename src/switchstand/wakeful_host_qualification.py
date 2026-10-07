"""Typed receipt for one exact Wakeful real-host journey; no product state."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from .stacked_delivery import EvidenceDimension, LayerQualification

HostVerdict = Literal["PASS", "FAIL", "UNKNOWN"]
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CONTROLS = ("launcher", "profile", "config", "qualifier")


@dataclass(frozen=True)
class HostJourney:
    candidate_sha: str
    expected_controls: tuple[tuple[str, str], ...]
    observed_controls: tuple[tuple[str, str], ...]
    nonce: str
    session_id: str | None
    observed_session_id: str | None
    observed_candidate_sha: str | None
    observed_nonce: str | None
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
    observation_error: str | None = None


@dataclass(frozen=True)
class HostQualification:
    verdict: HostVerdict
    reason: str
    evidence: LayerQualification
    control_identity: tuple[tuple[str, str], ...] | None


@dataclass(frozen=True)
class SameSessionTurnObservation:
    passed: bool | None
    observed_nonce: str | None
    error: str | None
    session_id: str | None


def observe_same_session_turn(
    thread: dict[str, Any], wake_id: str, nonce: str,
) -> SameSessionTurnObservation:
    """Distinguish durable queue consumption from an attributed assistant rollout."""

    session = thread.get("id")
    turns = thread.get("turns")
    if not isinstance(session, str) or not isinstance(turns, list):
        return SameSessionTurnObservation(None, None, "thread-observation-unavailable", None)
    matches: list[tuple[dict[str, Any], int]] = []
    for raw_turn in cast(list[Any], turns):
        if not isinstance(raw_turn, dict):
            return SameSessionTurnObservation(None, None, "thread-observation-unavailable", session)
        turn = cast(dict[str, Any], raw_turn)
        items = turn.get("items")
        if not isinstance(items, list):
            return SameSessionTurnObservation(None, None, "thread-observation-unavailable", session)
        for index, raw_item in enumerate(cast(list[Any], items)):
            item = cast(dict[str, Any], raw_item) if isinstance(raw_item, dict) else {}
            if item.get("type") == "userMessage" and item.get("clientId") == wake_id:
                matches.append((turn, index))
    if len(matches) != 1:
        return SameSessionTurnObservation(None, None, "wake-turn-ambiguous", session)
    turn, index = matches[0]
    replies: list[str] = []
    for raw_item in cast(list[Any], turn["items"])[index + 1:]:
        if isinstance(raw_item, dict):
            item = cast(dict[str, Any], raw_item)
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                replies.append(cast(str, item["text"]))
    if any(nonce in reply for reply in replies):
        return SameSessionTurnObservation(True, nonce, None, session)
    if turn.get("status") in {"completed", "failed", "cancelled"}:
        error = "nonce-not-observed" if replies else "zero-turn-no-rollout"
        return SameSessionTurnObservation(False, None, error, session)
    return SameSessionTurnObservation(None, None, "turn-incomplete", session)


def qualify_host_journey(journey: HostJourney) -> HostQualification:
    """Bind one claim-specific dimension to exact identity, stages, and zero residue."""

    dimension: frozenset[EvidenceDimension] = frozenset({"WAKEFUL_REAL_HOST"})

    def result(verdict: HostVerdict, reason: str) -> HostQualification:
        return HostQualification(verdict, reason, LayerQualification(
            journey.candidate_sha, dimension if verdict == "PASS" else frozenset(),
            known=verdict == "PASS",
        ), journey.observed_controls if verdict == "PASS" else None)

    identities = (journey.expected_controls, journey.observed_controls)
    if (_SHA.fullmatch(journey.candidate_sha) is None or len(journey.nonce) < 16 or any(
            tuple(name for name, _ in identity) != _CONTROLS
            or not all(_DIGEST.fullmatch(value) for _, value in identity)
            for identity in identities)):
        return result("UNKNOWN", "identity-unavailable")
    if journey.expected_controls != journey.observed_controls:
        return result("FAIL", "control-identity-mismatch")
    if journey.observation_error:
        failures = {"zero-turn-no-rollout", "nonce-not-observed"}
        known = failures | {"thread-observation-unavailable", "wake-turn-ambiguous", "turn-incomplete"}
        return result("FAIL" if journey.observation_error in failures else "UNKNOWN",
                      journey.observation_error if journey.observation_error in known
                      else "journey-evidence-malformed")
    binding = (journey.session_id, journey.observed_session_id,
               journey.observed_candidate_sha, journey.observed_nonce)
    if any(value is None for value in binding):
        return result("UNKNOWN", "journey-binding-unavailable")
    if binding != (journey.session_id, journey.session_id,
                   journey.candidate_sha, journey.nonce):
        return result("FAIL", "journey-binding-mismatch")
    stages = (journey.ordinary_dispatch_pilot, journey.persistent_host_pty,
              journey.exact_registration_binding, journey.remote_resume_succeeded,
              journey.nonce_committed, journey.visible_wake_without_stdin,
              journey.same_session_assistant_turn, journey.consumed)
    residues = (journey.queue_residue, journey.wake_residue, journey.source_result_residue)
    if any(value is not None and type(value) is not bool for value in stages) \
            or any(value is not None and type(value) is not int for value in residues):
        return result("UNKNOWN", "journey-evidence-malformed")
    if any(value is False for value in stages):
        return result("FAIL", "journey-stage-failed")
    if any(value not in {None, 0} for value in residues):
        return result("FAIL", "journey-residue")
    if any(value is None for value in (*stages, *residues)):
        return result("UNKNOWN", "journey-evidence-incomplete")
    return result("PASS", "complete-attributed-journey")
