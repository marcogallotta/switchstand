from dataclasses import replace
from datetime import UTC, datetime, timedelta

from switchstand.qualification_lineage import (
    AttemptScope,
    ObservationStatus,
    ProviderAttemptIdentity,
    QualificationAttempt,
    RetryReason,
    WarningDisposition,
    advisory_warning,
    qualification_lineage_id,
    summarize_lineage,
)

START = datetime(2026, 10, 7, 10, tzinfo=UTC)
LINEAGE = qualification_lineage_id("owner/repo", "cluster-1", "policy-v1")
SCOPE = AttemptScope("a" * 40, "b" * 40, "plan", "harness", "environment")


def attempt(number: int, **changes: object) -> QualificationAttempt:
    values: dict[str, object] = {
        "lineage_id": LINEAGE,
        "provider": ProviderAttemptIdentity("owner/repo", "Quality", "quality", 100, number),
        "scope": SCOPE,
        "retry_reason": RetryReason.INITIAL if number == 1 else RetryReason.HARNESS_REWORK,
        "started_at": START + timedelta(minutes=number - 1),
        "completed_at": START + timedelta(minutes=number),
        "foreground_seconds": 40,
        "harness_rework_seconds": 15 if number > 1 else 0,
        "wrapper_rework_seconds": 0,
        "fixture_rework_seconds": 0,
        "setup_rework_seconds": 0,
        "current": True,
    }
    values.update(changes)
    return QualificationAttempt(**values)  # type: ignore[arg-type]


def test_lineage_is_stable_while_provider_attempt_identity_is_idempotent() -> None:
    changed_candidate = AttemptScope("c" * 40, "b" * 40, "plan", "harness", "environment")
    assert qualification_lineage_id("owner/repo", "cluster-1", "policy-v1") == LINEAGE
    assert attempt(1).provider.executed_attempt_id == attempt(1).provider.executed_attempt_id
    assert attempt(1).provider.executed_attempt_id != attempt(2).provider.executed_attempt_id
    assert SCOPE.same_scope(changed_candidate) is False


def test_same_scope_requires_exact_subject_plan_harness_and_environment() -> None:
    fields = ("candidate_sha", "base_sha", "plan_digest", "harness_digest", "environment_digest")
    for field in fields:
        changed = replace(SCOPE, **{field: None if field == "environment_digest" else "different"})
        comparison = SCOPE.same_scope(changed)
        assert comparison is (None if field == "environment_digest" else False)


def test_cumulative_metrics_and_decisive_failure_timing() -> None:
    metrics = summarize_lineage((
        attempt(1, decisive_failure_at=START + timedelta(seconds=20)),
        attempt(2),
        attempt(3),
    ))
    assert metrics.status is ObservationStatus.AVAILABLE
    assert (
        metrics.attempt_count,
        metrics.cumulative_wall_seconds,
        metrics.cumulative_foreground_seconds,
        metrics.repeated_scope_retry_count,
        metrics.rework_seconds,
        metrics.time_to_first_decisive_failure_seconds,
        metrics.time_after_decisive_failure_seconds,
    ) == (3, 180, 120, 2, 30, 20, 160)


def test_missing_currentness_conflict_and_duplicate_attempt_are_unknown() -> None:
    assert summarize_lineage((attempt(1, current=None),)).reason == "currentness-conflict"
    assert summarize_lineage((attempt(1, foreground_seconds=None),)).reason == "evidence-missing"
    assert summarize_lineage((attempt(1), attempt(1))).attempt_count == 1
    duplicate = summarize_lineage((attempt(1), attempt(1, foreground_seconds=12)))
    assert (duplicate.status, duplicate.reason) == (ObservationStatus.UNKNOWN, "attempt-conflict")


def test_warning_is_advisory_once_per_lineage() -> None:
    metrics = summarize_lineage((attempt(1), attempt(2), attempt(3)))
    warning = advisory_warning(
        metrics, repeated_scope_threshold=2, rework_seconds_threshold=60,
    )
    assert (warning.disposition, warning.attention_kind, warning.route) == (
        WarningDisposition.EMIT,
        "CI_QUALIFICATION_REWORK_WARNING",
        "tests-ci-to-wakeful-attention",
    )
    assert warning.warning_id is not None
    assert advisory_warning(
        metrics,
        repeated_scope_threshold=2,
        rework_seconds_threshold=60,
        emitted_warning_ids=frozenset({warning.warning_id}),
    ).disposition is WarningDisposition.DEDUPED


def test_unknown_metrics_never_emit_warning() -> None:
    metrics = summarize_lineage((attempt(1, current=False),))
    assert advisory_warning(
        metrics, repeated_scope_threshold=0, rework_seconds_threshold=0,
    ).disposition is WarningDisposition.UNKNOWN
