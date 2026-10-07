from datetime import UTC, datetime, timedelta

from switchstand.full_suite_due import (
    BroadRun,
    FullSuiteInputs,
    FullSuitePolicy,
    evaluate_full_suite_due,
    observe_shadow,
    watermark_can_advance,
)

NOW = datetime(2026, 10, 7, tzinfo=UTC)
POLICY = FullSuitePolicy("adaptive-v1", 20, 24 * 60 * 60, 100)


def inputs(**changes: object) -> FullSuiteInputs:
    values: dict[str, object] = {
        "repository": "marcogallotta/switchstand",
        "landed_sha": "b" * 40,
        "baseline_sha": "a" * 40,
        "baseline_completed_at": NOW - timedelta(hours=1),
        "evaluated_at": NOW,
        "commits_since_baseline": 2,
        "changed_paths_since_baseline": 8,
        "ancestry_verified": True,
    }
    values.update(changes)
    return FullSuiteInputs(**values)  # type: ignore[arg-type]


def test_not_due_is_exact_and_deterministic() -> None:
    decision = evaluate_full_suite_due(inputs(), POLICY)
    assert decision.status == "NOT_DUE"
    assert decision.reasons == () and decision.due_event_id is None
    assert decision.subject_sha == "b" * 40


def test_each_backstop_and_policy_signal_is_due() -> None:
    cases = (
        (inputs(commits_since_baseline=20), "commit-budget-exhausted"),
        (inputs(changed_paths_since_baseline=100), "change-budget-exhausted"),
        (inputs(baseline_completed_at=NOW - timedelta(days=1)), "max-age-backstop"),
        (inputs(planner_policy_changed=True), "planner-policy-changed"),
        (inputs(unresolved_hard_miss=True), "unresolved-hard-miss"),
        (inputs(ancestry_verified=False), "baseline-not-ancestor"),
        (inputs(baseline_sha=None, baseline_completed_at=None), "trusted-baseline-missing"),
    )
    for current, reason in cases:
        decision = evaluate_full_suite_due(current, POLICY)
        assert decision.status == "FULL_SUITE_DUE"
        assert reason in decision.reasons
        assert decision.due_event_id is not None
        assert decision == evaluate_full_suite_due(current, POLICY)


def test_unknown_never_becomes_not_due() -> None:
    cases = (
        inputs(current_truth_known=False),
        inputs(ancestry_verified=None),
        inputs(commits_since_baseline=None),
        inputs(changed_paths_since_baseline=-1),
        inputs(landed_sha="not-a-sha"),
        inputs(baseline_completed_at=NOW + timedelta(seconds=1)),
    )
    assert all(evaluate_full_suite_due(value, POLICY).status == "UNKNOWN" for value in cases)


def test_known_non_ancestor_is_due_without_meaningless_diff_counters() -> None:
    decision = evaluate_full_suite_due(inputs(
        ancestry_verified=False,
        commits_since_baseline=None,
        changed_paths_since_baseline=None,
    ), POLICY)
    assert decision.status == "FULL_SUITE_DUE"
    assert decision.reasons == ("baseline-not-ancestor",)


def test_shadow_reuses_current_run_and_never_dispatches() -> None:
    decision = evaluate_full_suite_due(inputs(planner_policy_changed=True), POLICY)
    run = BroadRun("b" * 40, "in_progress")
    observed = observe_shadow(decision, (run,))
    assert observed.qualifying_run == run
    assert not observed.duplicate_dispatch_required
    assert not watermark_can_advance(decision, run)
    assert observe_shadow(decision, (BroadRun("c" * 40, "completed", "success"),)).qualifying_run is None


def test_only_due_exact_success_advances_watermark() -> None:
    due = evaluate_full_suite_due(inputs(unresolved_hard_miss=True), POLICY)
    success = BroadRun("b" * 40, "completed", "success")
    assert watermark_can_advance(due, success)
    assert not watermark_can_advance(evaluate_full_suite_due(inputs(), POLICY), success)
    assert not watermark_can_advance(due, BroadRun("b" * 40, "completed", "failure"))
    assert not watermark_can_advance(due, BroadRun("not-a-sha", "completed", "success"))
