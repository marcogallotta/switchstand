import json
from copy import deepcopy

from switchstand.flow_report import render

REPORT = {
    "schema": "switchstand.flow_report.v1",
    "status": "PARTIAL",
    "work_id": "10000000-0000-4000-8000-000000000001",
    "captured_at": "2026-01-02T03:04:05+00:00",
    "snapshot": {"isolation": "repeatable read", "read_only": "on", "row_version": 7},
    "admission": {"status": "UNKNOWN", "at": None, "reason": "NOT_CAPTURED"},
    "current": {
        "completed": False, "lifecycle_state": "UNKNOWN", "wait_kind": "UNKNOWN",
        "unblock_condition": "UNKNOWN", "next_due": "UNKNOWN",
        "next_action_class": "UNKNOWN", "next_action_ref": "UNKNOWN",
        "coverage": "CURRENT_ONLY",
    },
    "scope": {
        "canonical_root": {
            "status": "UNKNOWN", "work_id": None, "reason": "EXPLICIT_UNKNOWN",
        },
        "parent_ancestry": [], "parent_cycle_detected": True,
        "dependencies": [], "coverage": "CURRENT_ONLY",
    },
    "events": {"coverage": "OBSERVED_ONLY", "items": [{"id": "event"}]},
    "coverage": {
        "canonical_work": {"status": "INCLUDED", "reason": "CURRENT_ONLY"},
        "messages": {"status": "EXCLUDED", "reason": "AMBIGUOUS_ENDPOINT_NAMESPACE"},
        "timing_journal": {"status": "EXCLUDED", "reason": "RETENTION_NOT_PROVED"},
        "future_source": {"status": "EXCLUDED", "reason": "NOT_INCLUDED_B1"},
    },
    "elapsed": {
        "clock_basis": "RECORDED_WALL_TIME",
        "wall_status": "UNKNOWN", "wall_reason": "NOT_CAPTURED",
        "wall_start": None, "wall_end": "2026-01-02T03:04:05+00:00", "wall_ms": None,
        "projection_status": "UNKNOWN", "projection_reason": "NOT_CAPTURED",
        "observed_interval_union_ms": None, "unobserved_wall_ms": None,
        "unobserved_interpretation": "NOT_IDLE_OR_CRITICAL_PATH",
    },
}


def test_concise_render_preserves_partial_truth_and_every_coverage_reason() -> None:
    output = render(REPORT, "concise")

    assert output == """status=PARTIAL work_id=10000000-0000-4000-8000-000000000001 captured_at=2026-01-02T03:04:05+00:00
snapshot row_version=7 isolation=repeatable read read_only=on
admission status=UNKNOWN at=None reason=NOT_CAPTURED
current completed=False lifecycle_state=UNKNOWN wait_kind=UNKNOWN unblock_condition=UNKNOWN next_due=UNKNOWN next_action_class=UNKNOWN next_action_ref=UNKNOWN coverage=CURRENT_ONLY
root status=UNKNOWN work_id=None reason=EXPLICIT_UNKNOWN
relations parent_cycle_detected=True warning=CORRUPT_CYCLE
evidence_count=1 events_coverage=OBSERVED_ONLY
coverage canonical_work=INCLUDED:CURRENT_ONLY
coverage future_source=EXCLUDED:NOT_INCLUDED_B1
coverage messages=EXCLUDED:AMBIGUOUS_ENDPOINT_NAMESPACE
coverage timing_journal=EXCLUDED:RETENTION_NOT_PROVED
elapsed wall_status=UNKNOWN wall_reason=NOT_CAPTURED wall_ms=None projection_status=UNKNOWN projection_reason=NOT_CAPTURED observed_interval_union_ms=None unobserved_wall_ms=None interpretation=NOT_IDLE_OR_CRITICAL_PATH
"""


def test_json_remains_the_default_shape_without_renderer_inference() -> None:
    output = render(REPORT, "json")

    assert json.loads(output) == REPORT
    assert "NOT_IDLE_OR_CRITICAL_PATH" in output
    assert '"critical_path"' not in output


def test_concise_render_keeps_github_subjects_separate() -> None:
    value = deepcopy(REPORT)
    value["github"] = {
        "status": "OBSERVED", "reason": None, "correlation": "CALLER_SUPPLIED",
        "pull_request": 17, "expected_head_sha": "a" * 40,
        "observed_head_sha": "a" * 40, "qualification_status": "READY",
        "subjects": {
            "exact_head": {"status": "OBSERVED", "reason": None,
                           "subject_sha": "a" * 40, "intervals": [{}, {}],
                           "union_ms": 15_000},
            "composition": {"status": "OBSERVED", "reason": None,
                            "subject_sha": "b" * 40, "intervals": [{}],
                            "union_ms": 5_000},
        },
    }

    output = render(value, "concise")

    assert "github status=OBSERVED reason=None correlation=CALLER_SUPPLIED" in output
    assert "github_exact_head status=OBSERVED reason=None" in output
    assert "intervals=2 union_ms=15000" in output
    assert "github_composition status=OBSERVED reason=None" in output
    assert "intervals=1 union_ms=5000" in output


def test_concise_render_exposes_each_review_point_and_truthful_wait() -> None:
    value = deepcopy(REPORT)
    value["human_review"] = {
        "coverage": "DIRECT_PACKAGE_WORK_ID",
        "items": [
            {
                "consequence_id": "closed", "state": "READY_FOR_IMPLEMENTATION",
                "decision": "APPROVED", "prepared_at": "2026-01-02T01:00:00+00:00",
                "decided_at": "2026-01-02T02:00:00+00:00",
                "wait": {"kind": "REVIEW", "status": "CLOSED", "reason": None,
                         "start": "2026-01-02T01:00:00+00:00",
                         "end": "2026-01-02T02:00:00+00:00", "duration_ms": 3_600_000,
                         "clock_basis": "RECORDED_WALL_TIME"},
            },
            {
                "consequence_id": "open", "state": "PENDING", "decision": None,
                "prepared_at": "2026-01-02T02:00:00+00:00", "decided_at": None,
                "wait": {"kind": "REVIEW", "status": "OPEN", "reason": None,
                         "start": "2026-01-02T02:00:00+00:00",
                         "end": "2026-01-02T03:04:05+00:00", "duration_ms": 3_845_000,
                         "clock_basis": "RECORDED_WALL_TIME"},
            },
            {
                "consequence_id": "skew", "state": "HOLD", "decision": "HOLD",
                "prepared_at": "2026-01-02T04:00:00+00:00",
                "decided_at": "2026-01-02T03:00:00+00:00",
                "wait": {"kind": "REVIEW", "status": "UNKNOWN",
                         "reason": "CLOCK_SKEW_OR_NAIVE_TIMESTAMP",
                         "start": "2026-01-02T04:00:00+00:00",
                         "end": "2026-01-02T03:00:00+00:00", "duration_ms": None,
                         "clock_basis": "RECORDED_WALL_TIME"},
            },
        ],
    }

    output = render(value, "concise")

    assert "human_review_count=3 coverage=DIRECT_PACKAGE_WORK_ID" in output
    assert "human_review_point id=closed kind=PREPARED at=2026-01-02T01:00:00+00:00 duration_ms=0" in output
    assert "human_review_point id=closed kind=DECIDED at=2026-01-02T02:00:00+00:00 duration_ms=0" in output
    assert "human_review_wait id=closed kind=REVIEW status=CLOSED reason=None" in output
    assert "human_review_wait id=open kind=REVIEW status=OPEN reason=None" in output
    assert "human_review_wait id=skew kind=REVIEW status=UNKNOWN reason=CLOCK_SKEW_OR_NAIVE_TIMESTAMP" in output
    assert "duration_ms=None clock_basis=RECORDED_WALL_TIME" in output


def test_concise_render_preserves_outcome_currentness_and_unknown() -> None:
    value = deepcopy(REPORT)
    value["outcome_state"] = {
        "status": "KNOWN", "reason": None, "correlation": "DIRECT_OWNER_WORK_ID",
        "total_revisions": 2, "truncated": False,
        "revisions": [
            {"state_id": "first", "generation": 1, "schema_version": 1,
             "created_at": "2026-01-02T01:00:00+00:00", "currentness": "STALE",
             "item_status_counts": {
                 "NOT_STARTED": 0, "READY": 1, "IN_PROGRESS": 0, "DONE": 0,
             }},
            {"state_id": "second", "generation": 2, "schema_version": 1,
             "created_at": "2026-01-02T02:00:00+00:00", "currentness": "CURRENT",
             "item_status_counts": {
                 "NOT_STARTED": 0, "READY": 0, "IN_PROGRESS": 1, "DONE": 1,
             }},
        ],
    }

    output = render(value, "concise")

    assert "outcome_state status=KNOWN reason=None correlation=DIRECT_OWNER_WORK_ID" in output
    assert "outcome_revision generation=1 state_id=first" in output
    assert "currentness=STALE" in output and "currentness=CURRENT" in output
    assert "counts=NOT_STARTED:0,READY:0,IN_PROGRESS:1,DONE:1" in output

    value["outcome_state"] = {
        "status": "UNKNOWN", "reason": "CORRUPT_REVISION_CHAIN",
        "correlation": "DIRECT_OWNER_WORK_ID", "total_revisions": None,
        "truncated": None, "revisions": [],
    }
    unknown = render(value, "concise")
    assert "outcome_state status=UNKNOWN reason=CORRUPT_REVISION_CHAIN" in unknown


def test_concise_render_preserves_safe_trajectory_headers_and_unknown() -> None:
    value = deepcopy(REPORT)
    value["human_trajectory"] = {
        "status": "KNOWN", "reason": None, "chain_status": "VALIDATED",
        "correlation": "DIRECT_WORK_ID_REF", "total_revisions": 1,
        "truncated": False,
        "revisions": [{
            "trajectory_id": "safe-id", "generation": 3,
            "source_kind": "HUMAN_REVIEW",
            "created_at": "2026-01-02T02:00:00+00:00",
        }],
    }

    output = render(value, "concise")

    assert "human_trajectory status=KNOWN reason=None chain_status=VALIDATED" in output
    assert "trajectory_revision generation=3 trajectory_id=safe-id" in output
    assert "source_kind=HUMAN_REVIEW created_at=2026-01-02T02:00:00+00:00" in output
    assert "CURRENT" not in "\n".join(
        line for line in output.splitlines() if "trajectory" in line
    )

    value["human_trajectory"] = {
        "status": "UNKNOWN", "reason": "CLOCK_SKEW_OR_FUTURE_CREATED_AT",
        "chain_status": "UNKNOWN", "correlation": "DIRECT_WORK_ID_REF",
        "total_revisions": None, "truncated": None, "revisions": [],
    }
    unknown = render(value, "concise")
    assert "human_trajectory status=UNKNOWN reason=CLOCK_SKEW_OR_FUTURE_CREATED_AT" in unknown
