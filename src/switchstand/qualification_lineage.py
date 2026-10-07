"""Advisory qualification-attempt lineage and rework observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from typing import cast


class RetryReason(StrEnum):
    INITIAL = "INITIAL"
    PROVIDER_RETRY = "PROVIDER_RETRY"
    CANDIDATE_REWORK = "CANDIDATE_REWORK"
    HARNESS_REWORK = "HARNESS_REWORK"
    WRAPPER_REWORK = "WRAPPER_REWORK"
    FIXTURE_REWORK = "FIXTURE_REWORK"
    SETUP_REWORK = "SETUP_REWORK"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    UNKNOWN = "UNKNOWN"


class ObservationStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNKNOWN = "UNKNOWN"


class WarningDisposition(StrEnum):
    EMIT = "EMIT"
    DEDUPED = "DEDUPED"
    NOT_DUE = "NOT_DUE"
    UNKNOWN = "UNKNOWN"


def _stable_id(kind: str, value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"{kind}-{hashlib.sha256(encoded).hexdigest()}"


def qualification_lineage_id(repository: str, concern: str, policy_generation: str) -> str:
    """Identify a logical qualification across candidate revisions."""

    if not all((repository, concern, policy_generation)):
        raise ValueError("lineage identity fields must be non-empty")
    return _stable_id("ql", (repository, concern, policy_generation))


@dataclass(frozen=True, slots=True)
class ProviderAttemptIdentity:
    repository: str
    workflow: str
    job: str
    run_id: int
    run_attempt: int

    def __post_init__(self) -> None:
        if not all((self.repository, self.workflow, self.job)):
            raise ValueError("provider identity fields must be non-empty")
        if self.run_id <= 0 or self.run_attempt <= 0:
            raise ValueError("provider run identity must be positive")

    @property
    def executed_attempt_id(self) -> str:
        return _stable_id(
            "qa", (self.repository, self.workflow, self.job, self.run_id, self.run_attempt)
        )


@dataclass(frozen=True, slots=True)
class AttemptScope:
    candidate_sha: str | None
    base_sha: str | None
    plan_digest: str | None
    harness_digest: str | None
    environment_digest: str | None

    def same_scope(self, other: AttemptScope) -> bool | None:
        if any(value is None or value == "" for value in (*self.identity, *other.identity)):
            return None
        return self == other

    @property
    def identity(self) -> tuple[str | None, ...]:
        return (
            self.candidate_sha, self.base_sha, self.plan_digest,
            self.harness_digest, self.environment_digest,
        )


@dataclass(frozen=True, slots=True)
class QualificationAttempt:
    lineage_id: str
    provider: ProviderAttemptIdentity
    scope: AttemptScope
    retry_reason: RetryReason
    started_at: datetime | None
    completed_at: datetime | None
    decisive_failure_at: datetime | None = None
    foreground_seconds: float | None = None
    harness_rework_seconds: float | None = None
    wrapper_rework_seconds: float | None = None
    fixture_rework_seconds: float | None = None
    setup_rework_seconds: float | None = None
    current: bool | None = None

    def __post_init__(self) -> None:
        for field in ("started_at", "completed_at", "decisive_failure_at"):
            value = getattr(self, field)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{field} must be timezone-aware")
            if value is not None:
                object.__setattr__(self, field, value.astimezone(UTC))
        for field in (
            "foreground_seconds", "harness_rework_seconds", "wrapper_rework_seconds",
            "fixture_rework_seconds", "setup_rework_seconds",
        ):
            value = getattr(self, field)
            if value is not None and value < 0:
                raise ValueError(f"{field} cannot be negative")


@dataclass(frozen=True, slots=True)
class LineageMetrics:
    status: ObservationStatus
    lineage_id: str
    attempt_count: int | None = None
    cumulative_wall_seconds: float | None = None
    cumulative_foreground_seconds: float | None = None
    repeated_scope_retry_count: int | None = None
    rework_seconds: float | None = None
    time_to_first_decisive_failure_seconds: float | None = None
    time_after_decisive_failure_seconds: float | None = None
    reason: str | None = None


def summarize_lineage(attempts: tuple[QualificationAttempt, ...]) -> LineageMetrics:
    if not attempts:
        raise ValueError("at least one attempt is required")
    lineage = attempts[0].lineage_id
    if any(item.lineage_id != lineage for item in attempts):
        return LineageMetrics(ObservationStatus.UNKNOWN, lineage, reason="lineage-conflict")
    unique: dict[str, QualificationAttempt] = {}
    for item in attempts:
        attempt_id = item.provider.executed_attempt_id
        prior = unique.get(attempt_id)
        if prior is not None and prior != item:
            return LineageMetrics(ObservationStatus.UNKNOWN, lineage, reason="attempt-conflict")
        unique[attempt_id] = item
    observed = tuple(unique.values())
    if any(item.current is not True for item in observed):
        return LineageMetrics(ObservationStatus.UNKNOWN, lineage, reason="currentness-conflict")
    if any(
        item.started_at is None or item.completed_at is None
        or any(value is None or value == "" for value in item.scope.identity)
        or any(getattr(item, field) is None for field in (
            "foreground_seconds", "harness_rework_seconds", "wrapper_rework_seconds",
            "fixture_rework_seconds", "setup_rework_seconds",
        ))
        for item in observed
    ):
        return LineageMetrics(ObservationStatus.UNKNOWN, lineage, reason="evidence-missing")
    ordered = sorted(observed, key=lambda item: (item.started_at, item.provider.executed_attempt_id))
    starts = [cast(datetime, item.started_at) for item in ordered]
    ends = [cast(datetime, item.completed_at) for item in ordered]
    if any(end < start for start, end in zip(starts, ends, strict=True)):
        return LineageMetrics(ObservationStatus.UNKNOWN, lineage, reason="chronology-conflict")
    same_scope: list[bool] = []
    for prior, current in pairwise(ordered):
        comparison = prior.scope.same_scope(current.scope)
        if comparison is None:
            return LineageMetrics(ObservationStatus.UNKNOWN, lineage, reason="scope-unknown")
        same_scope.append(comparison)
    failures: list[datetime] = []
    for item, start, end in zip(ordered, starts, ends, strict=True):
        if item.decisive_failure_at is not None:
            if not start <= item.decisive_failure_at <= end:
                return LineageMetrics(
                    ObservationStatus.UNKNOWN, lineage, reason="failure-time-conflict"
                )
            failures.append(item.decisive_failure_at)
    failures.sort()
    first_start = starts[0]
    last_end = max(ends)
    first_failure = failures[0] if failures else None
    foreground = [cast(float, item.foreground_seconds) for item in ordered]
    rework = sum(
        cast(float, getattr(item, field))
        for item in ordered
        for field in (
            "harness_rework_seconds", "wrapper_rework_seconds",
            "fixture_rework_seconds", "setup_rework_seconds",
        )
    )
    return LineageMetrics(
        ObservationStatus.AVAILABLE,
        lineage,
        attempt_count=len(ordered),
        cumulative_wall_seconds=sum(
            (end - start).total_seconds() for start, end in zip(starts, ends, strict=True)
        ),
        cumulative_foreground_seconds=sum(foreground),
        repeated_scope_retry_count=sum(same_scope),
        rework_seconds=rework,
        time_to_first_decisive_failure_seconds=(
            (first_failure - first_start).total_seconds() if first_failure else None
        ),
        time_after_decisive_failure_seconds=(
            (last_end - first_failure).total_seconds() if first_failure else None
        ),
    )


@dataclass(frozen=True, slots=True)
class ReworkWarning:
    disposition: WarningDisposition
    warning_id: str | None
    attention_kind: str = "CI_QUALIFICATION_REWORK_WARNING"
    route: str = "tests-ci-to-wakeful-attention"


def advisory_warning(
    metrics: LineageMetrics,
    *,
    repeated_scope_threshold: int,
    rework_seconds_threshold: float,
    emitted_warning_ids: frozenset[str] = frozenset(),
) -> ReworkWarning:
    """Return an inert warning request; an existing attention adapter owns delivery."""

    if repeated_scope_threshold < 0 or rework_seconds_threshold < 0:
        raise ValueError("advisory warning thresholds cannot be negative")
    if metrics.status is ObservationStatus.UNKNOWN:
        return ReworkWarning(WarningDisposition.UNKNOWN, None)
    warning_id = _stable_id("qw", (metrics.lineage_id, "CI_QUALIFICATION_REWORK_WARNING"))
    if warning_id in emitted_warning_ids:
        return ReworkWarning(WarningDisposition.DEDUPED, warning_id)
    due = (
        metrics.repeated_scope_retry_count is not None
        and metrics.repeated_scope_retry_count >= repeated_scope_threshold
    ) or (
        metrics.rework_seconds is not None
        and metrics.rework_seconds >= rework_seconds_threshold
    )
    return ReworkWarning(
        WarningDisposition.EMIT if due else WarningDisposition.NOT_DUE, warning_id
    )
