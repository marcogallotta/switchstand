from datetime import UTC, datetime, timedelta

import pytest

from switchstand.ci_runtime_budget import (
    BudgetStatus,
    ProviderProgress,
    ProviderRunStatus,
    RunBudgetPolicy,
    RunClass,
    RunRuntimeState,
    UnknownReason,
    evaluate_runtime_budget,
)

START = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def policy(**changes: object) -> RunBudgetPolicy:
    values: dict[str, object] = {
        "repository": "marcogallotta/switchstand",
        "workflow": "Quality",
        "job": "quality",
        "run_class": RunClass.FOREGROUND_QUALIFICATION,
        "expected_seconds": 240,
        "warning_seconds": 300,
        "provisional_future_hard_seconds": 600,
        "policy_version": "runtime-v1",
        "policy_source": "human-review:34ee9aeb",
    }
    values.update(changes)
    return RunBudgetPolicy(**values)  # type: ignore[arg-type]


def state(**changes: object) -> RunRuntimeState:
    values: dict[str, object] = {
        "repository": "marcogallotta/switchstand",
        "workflow": "Quality",
        "job": "quality",
        "run_id": 37221244818,
        "run_attempt": 1,
        "status": ProviderRunStatus.IN_PROGRESS,
        "progress": ProviderProgress.IN_PROGRESS,
        "started_at": START,
        "observed_at": START + timedelta(seconds=299),
    }
    values.update(changes)
    return RunRuntimeState(**values)  # type: ignore[arg-type]


def test_warning_boundary_is_advisory_breach_and_second_before_is_within() -> None:
    subject = policy()

    before = evaluate_runtime_budget(state(), (subject,))
    boundary = evaluate_runtime_budget(
        state(observed_at=START + timedelta(seconds=300)), (subject,)
    )

    assert (before.status, before.elapsed_seconds) == (BudgetStatus.WITHIN, 299)
    assert (boundary.status, boundary.elapsed_seconds) == (BudgetStatus.BREACH, 300)
    assert boundary.policy_version == "runtime-v1"


@pytest.mark.parametrize(
    ("status", "progress"),
    [
        (ProviderRunStatus.REQUESTED, ProviderProgress.REQUESTED),
        (ProviderRunStatus.COMPLETED, ProviderProgress.COMPLETED),
    ],
)
def test_nonrunning_run_never_creates_new_warning(
    status: ProviderRunStatus, progress: ProviderProgress
) -> None:
    result = evaluate_runtime_budget(
        state(
            status=status,
            progress=progress,
            observed_at=START + timedelta(seconds=900),
        ),
        (policy(),),
    )

    assert result.status is BudgetStatus.WITHIN
    assert result.elapsed_seconds is None


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"status": ProviderRunStatus.UNKNOWN}, UnknownReason.PROVIDER_STATUS_UNKNOWN),
        ({"progress": ProviderProgress.UNKNOWN}, UnknownReason.PROVIDER_PROGRESS_UNKNOWN),
        ({"started_at": None}, UnknownReason.START_UNKNOWN),
        ({"observed_at": None}, UnknownReason.OBSERVATION_UNKNOWN),
        (
            {"observed_at": START - timedelta(seconds=1)},
            UnknownReason.CLOCK_INCONSISTENT,
        ),
    ],
)
def test_ambiguous_provider_or_timing_state_is_unknown(
    changes: dict[str, object], reason: UnknownReason
) -> None:
    result = evaluate_runtime_budget(state(**changes), (policy(),))

    assert (result.status, result.reason, result.elapsed_seconds) == (
        BudgetStatus.UNKNOWN,
        reason,
        None,
    )


def test_only_exact_allowlisted_selector_is_managed() -> None:
    policies = (policy(),)

    for changed in (
        {"repository": "marcogallotta/codex"},
        {"workflow": "Quality nightly"},
        {"job": "quality-2"},
    ):
        result = evaluate_runtime_budget(state(**changed), policies)
        assert (result.status, result.reason, result.policy_version) == (
            BudgetStatus.UNKNOWN,
            UnknownReason.UNMANAGED,
            None,
        )


def test_duplicate_policy_is_ambiguous_not_first_match_wins() -> None:
    result = evaluate_runtime_budget(state(), (policy(), policy(policy_version="runtime-v2")))

    assert (result.status, result.reason) == (
        BudgetStatus.UNKNOWN,
        UnknownReason.POLICY_AMBIGUOUS,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_seconds": 0},
        {"expected_seconds": 301},
        {"provisional_future_hard_seconds": 299},
    ],
)
def test_policy_rejects_invalid_advisory_order(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="expected <= warning <= future hard"):
        policy(**changes)


def test_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="observed_at must be timezone-aware"):
        state(observed_at=datetime(2026, 10, 4, 12, 5, tzinfo=UTC).replace(tzinfo=None))


def test_provisional_hard_field_does_not_enforce_an_effect() -> None:
    subject = policy(provisional_future_hard_seconds=600)
    result = evaluate_runtime_budget(
        state(observed_at=START + timedelta(seconds=3600)), (subject,)
    )

    assert result.status is BudgetStatus.BREACH
    assert result.elapsed_seconds == 3600
