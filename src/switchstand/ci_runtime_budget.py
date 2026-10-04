"""Pure advisory evaluation for explicitly managed in-progress CI runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class RunClass(StrEnum):
    FOREGROUND_QUALIFICATION = "FOREGROUND_QUALIFICATION"
    AUTHORITATIVE_QUALITY = "AUTHORITATIVE_QUALITY"
    BROAD_REGRESSION = "BROAD_REGRESSION"


class ProviderRunStatus(StrEnum):
    """Lifecycle of the exact selected execution (workflow job)."""

    REQUESTED = "REQUESTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    UNKNOWN = "UNKNOWN"


class ProviderProgress(StrEnum):
    """Provider-visible subordinate job/step progress, not a duplicate run status."""

    REQUESTED = "REQUESTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    UNKNOWN = "UNKNOWN"


class BudgetStatus(StrEnum):
    WITHIN = "WITHIN"
    BREACH = "BREACH"
    UNKNOWN = "UNKNOWN"


class UnknownReason(StrEnum):
    UNMANAGED = "UNMANAGED"
    POLICY_AMBIGUOUS = "POLICY_AMBIGUOUS"
    PROVIDER_STATUS_UNKNOWN = "PROVIDER_STATUS_UNKNOWN"
    PROVIDER_PROGRESS_UNKNOWN = "PROVIDER_PROGRESS_UNKNOWN"
    PROVIDER_STATE_INCONSISTENT = "PROVIDER_STATE_INCONSISTENT"
    START_UNKNOWN = "START_UNKNOWN"
    OBSERVATION_UNKNOWN = "OBSERVATION_UNKNOWN"
    CLOCK_INCONSISTENT = "CLOCK_INCONSISTENT"


def _single_line(value: str, field: str) -> None:
    if not value or len(value) > 200 or "\n" in value or "\r" in value:
        raise ValueError(f"{field} must be a non-empty single line of at most 200 characters")


def _utc(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class RunBudgetPolicy:
    repository: str
    workflow: str
    job: str
    run_class: RunClass
    expected_seconds: int
    warning_seconds: int
    provisional_future_hard_seconds: int
    policy_version: str
    policy_source: str

    def __post_init__(self) -> None:
        for field in ("repository", "workflow", "job", "policy_version", "policy_source"):
            _single_line(str(getattr(self, field)), field)
        if not (
            0 < self.expected_seconds
            <= self.warning_seconds
            <= self.provisional_future_hard_seconds
        ):
            raise ValueError(
                "advisory seconds must be positive and ordered expected <= warning <= future hard"
            )

    @property
    def selector(self) -> tuple[str, str, str]:
        return self.repository, self.workflow, self.job


@dataclass(frozen=True, slots=True)
class RunRuntimeState:
    repository: str
    workflow: str
    job: str
    run_id: int
    run_attempt: int
    status: ProviderRunStatus
    progress: ProviderProgress
    started_at: datetime | None
    observed_at: datetime | None

    def __post_init__(self) -> None:
        for field in ("repository", "workflow", "job"):
            _single_line(str(getattr(self, field)), field)
        if self.run_id <= 0 or self.run_attempt <= 0:
            raise ValueError("run_id and run_attempt must be positive")
        object.__setattr__(self, "started_at", _utc(self.started_at, "started_at"))
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))

    @property
    def selector(self) -> tuple[str, str, str]:
        return self.repository, self.workflow, self.job


@dataclass(frozen=True, slots=True)
class BudgetResult:
    status: BudgetStatus
    policy_version: str | None
    elapsed_seconds: int | None
    reason: UnknownReason | None = None

    def __post_init__(self) -> None:
        if (self.status is BudgetStatus.UNKNOWN) != (self.reason is not None):
            raise ValueError("UNKNOWN results require a reason and known results forbid one")
        if self.elapsed_seconds is not None and self.elapsed_seconds < 0:
            raise ValueError("elapsed_seconds cannot be negative")


def evaluate_runtime_budget(
    state: RunRuntimeState,
    policies: tuple[RunBudgetPolicy, ...],
) -> BudgetResult:
    """Evaluate active warning state without producing an effect.

    Requested and completed runs never create a new in-progress warning. The adapter that
    eventually persists warnings owns deduplication and preservation of an earlier breach.
    """
    matches = tuple(policy for policy in policies if policy.selector == state.selector)
    if not matches:
        return BudgetResult(BudgetStatus.UNKNOWN, None, None, UnknownReason.UNMANAGED)
    if len(matches) > 1:
        return BudgetResult(BudgetStatus.UNKNOWN, None, None, UnknownReason.POLICY_AMBIGUOUS)
    policy = next(iter(matches))
    if state.status is ProviderRunStatus.UNKNOWN:
        return BudgetResult(
            BudgetStatus.UNKNOWN,
            policy.policy_version,
            None,
            UnknownReason.PROVIDER_STATUS_UNKNOWN,
        )
    if state.progress is ProviderProgress.UNKNOWN:
        return BudgetResult(
            BudgetStatus.UNKNOWN,
            policy.policy_version,
            None,
            UnknownReason.PROVIDER_PROGRESS_UNKNOWN,
        )
    inconsistent = (
        state.status is ProviderRunStatus.REQUESTED
        and state.progress is not ProviderProgress.REQUESTED
    ) or (
        state.status is ProviderRunStatus.COMPLETED
        and state.progress is not ProviderProgress.COMPLETED
    )
    if inconsistent:
        return BudgetResult(
            BudgetStatus.UNKNOWN,
            policy.policy_version,
            None,
            UnknownReason.PROVIDER_STATE_INCONSISTENT,
        )
    if state.status in {ProviderRunStatus.REQUESTED, ProviderRunStatus.COMPLETED}:
        return BudgetResult(BudgetStatus.WITHIN, policy.policy_version, None)
    if state.started_at is None:
        return BudgetResult(
            BudgetStatus.UNKNOWN, policy.policy_version, None, UnknownReason.START_UNKNOWN
        )
    if state.observed_at is None:
        return BudgetResult(
            BudgetStatus.UNKNOWN, policy.policy_version, None, UnknownReason.OBSERVATION_UNKNOWN
        )
    raw_elapsed = (state.observed_at - state.started_at).total_seconds()
    if raw_elapsed < 0:
        return BudgetResult(
            BudgetStatus.UNKNOWN, policy.policy_version, None, UnknownReason.CLOCK_INCONSISTENT
        )
    elapsed = int(raw_elapsed)
    status = (
        BudgetStatus.BREACH if elapsed >= policy.warning_seconds else BudgetStatus.WITHIN
    )
    return BudgetResult(status, policy.policy_version, elapsed)
