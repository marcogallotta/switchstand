"""Pure, shadow-only adaptive broad-suite policy.

The evaluator records when broad truth is due.  It does not dispatch CI, advance
watermarks, or reduce the existing scheduled/default-branch cadence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

DueStatus = Literal["FULL_SUITE_DUE", "NOT_DUE", "UNKNOWN"]
_SHA = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class FullSuitePolicy:
    max_commits: int
    max_age_seconds: int
    max_changed_paths: int


@dataclass(frozen=True)
class FullSuiteInputs:
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
    subject_sha: str | None


def _due(inputs: FullSuiteInputs, reasons: tuple[str, ...] | list[str]) -> FullSuiteDecision:
    return FullSuiteDecision("FULL_SUITE_DUE", tuple(sorted(reasons)), inputs.landed_sha)


def evaluate_full_suite_due(
    inputs: FullSuiteInputs, policy: FullSuitePolicy,
) -> FullSuiteDecision:
    """Return deterministic broad-truth due state without causing an effect."""

    values_valid = (
        _SHA.fullmatch(inputs.landed_sha) is not None
        and policy.max_commits > 0
        and policy.max_age_seconds > 0
        and policy.max_changed_paths > 0
        and inputs.evaluated_at.tzinfo is not None
    )
    if not values_valid or not inputs.current_truth_known:
        return FullSuiteDecision("UNKNOWN", ("current-inputs-unavailable",), None)

    if inputs.baseline_sha is None or inputs.baseline_completed_at is None:
        return _due(inputs, ("trusted-baseline-missing",))
    commits = inputs.commits_since_baseline
    changed_paths = inputs.changed_paths_since_baseline
    counters_known = commits is not None and commits >= 0 \
        and changed_paths is not None and changed_paths >= 0
    if (
        _SHA.fullmatch(inputs.baseline_sha) is None
        or inputs.baseline_completed_at.tzinfo is None
        or inputs.ancestry_verified is None
    ):
        return FullSuiteDecision("UNKNOWN", ("current-inputs-unavailable",), None)
    if not inputs.ancestry_verified:
        return _due(inputs, ("baseline-not-ancestor",))
    if not counters_known:
        return FullSuiteDecision("UNKNOWN", ("current-inputs-unavailable",), None)
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
        return FullSuiteDecision("UNKNOWN", ("contradictory-timestamps",), None)
    if age.total_seconds() >= policy.max_age_seconds:
        reasons.append("max-age-backstop")
    if reasons:
        return _due(inputs, reasons)
    return FullSuiteDecision("NOT_DUE", (), inputs.landed_sha)
