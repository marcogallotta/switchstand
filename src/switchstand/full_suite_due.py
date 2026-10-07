"""Pure, shadow-only adaptive broad-suite policy.

The evaluator records when broad truth is due.  It does not dispatch CI, advance
watermarks, or reduce the existing scheduled/default-branch cadence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

DueStatus = Literal["FULL_SUITE_DUE", "NOT_DUE", "UNKNOWN"]
RunState = Literal["scheduled", "in_progress", "completed"]
_SHA = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class FullSuitePolicy:
    epoch: str
    max_commits: int
    max_age_seconds: int
    max_changed_paths: int


@dataclass(frozen=True)
class FullSuiteInputs:
    repository: str
    landed_sha: str
    baseline_sha: str | None
    baseline_completed_at: datetime | None
    evaluated_at: datetime
    commits_since_baseline: int | None
    changed_paths_since_baseline: int | None
    ancestry_verified: bool | None
    planner_policy_changed: bool = False
    unresolved_hard_miss: bool = False
    current_truth_known: bool = True


@dataclass(frozen=True)
class FullSuiteDecision:
    status: DueStatus
    reasons: tuple[str, ...]
    due_event_id: str | None
    subject_sha: str | None


@dataclass(frozen=True)
class BroadRun:
    subject_sha: str
    state: RunState
    conclusion: str | None = None


@dataclass(frozen=True)
class ShadowObservation:
    decision: FullSuiteDecision
    qualifying_run: BroadRun | None
    duplicate_dispatch_required: bool = False


def _event_id(inputs: FullSuiteInputs, policy: FullSuitePolicy, reasons: Sequence[str]) -> str:
    encoded = json.dumps(
        {
            "repository": inputs.repository,
            "landed_sha": inputs.landed_sha,
            "policy_epoch": policy.epoch,
            "request_class": "full-suite",
            "reasons": sorted(reasons),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def evaluate_full_suite_due(
    inputs: FullSuiteInputs, policy: FullSuitePolicy,
) -> FullSuiteDecision:
    """Return deterministic broad-truth due state without causing an effect."""

    values_valid = (
        bool(inputs.repository)
        and bool(policy.epoch)
        and _SHA.fullmatch(inputs.landed_sha) is not None
        and policy.max_commits > 0
        and policy.max_age_seconds > 0
        and policy.max_changed_paths > 0
        and inputs.evaluated_at.tzinfo is not None
    )
    if not values_valid or not inputs.current_truth_known:
        return FullSuiteDecision("UNKNOWN", ("current-inputs-unavailable",), None, None)

    if inputs.baseline_sha is None or inputs.baseline_completed_at is None:
        missing_reasons = ("trusted-baseline-missing",)
        return FullSuiteDecision(
            "FULL_SUITE_DUE", missing_reasons,
            _event_id(inputs, policy, missing_reasons), inputs.landed_sha,
        )
    commits = inputs.commits_since_baseline
    changed_paths = inputs.changed_paths_since_baseline
    counters_known = (
        commits is not None
        and changed_paths is not None
        and commits >= 0
        and changed_paths >= 0
    )
    if (
        _SHA.fullmatch(inputs.baseline_sha) is None
        or inputs.baseline_completed_at.tzinfo is None
        or inputs.ancestry_verified is None
    ):
        return FullSuiteDecision("UNKNOWN", ("current-inputs-unavailable",), None, None)
    if not inputs.ancestry_verified:
        nonancestor_reasons = ("baseline-not-ancestor",)
        return FullSuiteDecision(
            "FULL_SUITE_DUE", nonancestor_reasons,
            _event_id(inputs, policy, nonancestor_reasons), inputs.landed_sha,
        )
    if not counters_known:
        return FullSuiteDecision("UNKNOWN", ("current-inputs-unavailable",), None, None)
    assert commits is not None and changed_paths is not None

    reasons: list[str] = []
    if inputs.planner_policy_changed:
        reasons.append("planner-policy-changed")
    if inputs.unresolved_hard_miss:
        reasons.append("unresolved-hard-miss")
    if commits >= policy.max_commits:
        reasons.append("commit-budget-exhausted")
    if changed_paths >= policy.max_changed_paths:
        reasons.append("change-budget-exhausted")
    age = inputs.evaluated_at.astimezone(UTC) - inputs.baseline_completed_at.astimezone(UTC)
    if age.total_seconds() < 0:
        return FullSuiteDecision("UNKNOWN", ("contradictory-timestamps",), None, None)
    if age.total_seconds() >= policy.max_age_seconds:
        reasons.append("max-age-backstop")
    if reasons:
        ordered = tuple(sorted(reasons))
        return FullSuiteDecision(
            "FULL_SUITE_DUE", ordered, _event_id(inputs, policy, ordered), inputs.landed_sha
        )
    return FullSuiteDecision("NOT_DUE", (), None, inputs.landed_sha)


def observe_shadow(decision: FullSuiteDecision, runs: Sequence[BroadRun]) -> ShadowObservation:
    """Correlate current broad CI without ever requesting a second suite."""

    matching = tuple(run for run in runs if run.subject_sha == decision.subject_sha)
    qualifying = next(
        (
            run for run in matching
            if run.state in {"scheduled", "in_progress"}
            or (run.state == "completed" and run.conclusion == "success")
        ),
        None,
    )
    return ShadowObservation(decision, qualifying)


def watermark_can_advance(decision: FullSuiteDecision, run: BroadRun | None) -> bool:
    """Advance only from due to exact successful broad evidence."""

    return bool(
        decision.status == "FULL_SUITE_DUE"
        and run is not None
        and run.state == "completed"
        and run.conclusion == "success"
        and run.subject_sha == decision.subject_sha
    )
