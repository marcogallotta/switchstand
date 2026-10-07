from datetime import UTC, datetime, timedelta

from switchstand.full_suite_due import FullSuiteInputs, FullSuitePolicy, evaluate_full_suite_due

NOW = datetime(2026, 10, 7, tzinfo=UTC)
POLICY = FullSuitePolicy(20, 24 * 60 * 60, 100, 2_000, 50, 0.5)


def inputs(**changes: object) -> FullSuiteInputs:
    values: dict[str, object] = {
        "landed_sha": "b" * 40,
        "baseline_sha": "a" * 40, "baseline_completed_at": NOW - timedelta(hours=1),
        "evaluated_at": NOW,
        "commits_since_baseline": 2, "changed_production_files": 8,
        "changed_production_lines": 100, "selected_tests": 4, "total_tests": 100,
        "ancestry_verified": True,
        "planner_mode": "SELECTED", "high_risk_fallback": False,
        "planner_policy_changed": False, "unresolved_hard_miss": False,
    }
    values.update(changes)
    return FullSuiteInputs(**values)  # type: ignore[arg-type]


def test_not_due_is_exact_and_deterministic() -> None:
    decision = evaluate_full_suite_due(inputs(), POLICY)
    assert decision.status == "NOT_DUE"
    assert decision.reasons == ()
    assert decision.subject_sha == "b" * 40


def test_each_backstop_and_policy_signal_is_due() -> None:
    cases = (
        (inputs(commits_since_baseline=20), "commit-budget-exhausted"),
        (inputs(changed_production_files=100), "production-file-budget-exhausted"),
        (inputs(changed_production_lines=2_000), "production-line-budget-exhausted"),
        (inputs(selected_tests=50), "selected-test-count-budget-exhausted"),
        (inputs(selected_tests=50, total_tests=100), "selected-test-ratio-budget-exhausted"),
        (inputs(planner_mode="FULL_FALLBACK"), "planner-full-fallback"),
        (inputs(planner_mode="NO_PLAN"), "planner-no-plan"),
        (inputs(planner_mode="STALE"), "planner-stale"),
        (inputs(high_risk_fallback=True), "high-risk-fallback"),
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
        assert decision == evaluate_full_suite_due(current, POLICY)


def test_unknown_never_becomes_not_due() -> None:
    cases = (
        inputs(current_truth_known=False),
        inputs(ancestry_verified=None),
        inputs(commits_since_baseline=None),
        inputs(changed_production_files=-1),
        inputs(changed_production_lines=None),
        inputs(selected_tests=101, total_tests=100),
        inputs(planner_mode=None),
        inputs(high_risk_fallback=None),
        inputs(landed_sha="not-a-sha"),
        inputs(baseline_completed_at=NOW + timedelta(seconds=1)),
        inputs(planner_policy_changed=None),
        inputs(unresolved_hard_miss=None),
    )
    assert all(evaluate_full_suite_due(value, POLICY).status == "UNKNOWN" for value in cases)


def test_known_non_ancestor_is_due_without_meaningless_diff_counters() -> None:
    decision = evaluate_full_suite_due(inputs(
        ancestry_verified=False,
        commits_since_baseline=None,
        changed_production_files=None,
    ), POLICY)
    assert decision.status == "FULL_SUITE_DUE"
    assert decision.reasons == ("baseline-not-ancestor",)
