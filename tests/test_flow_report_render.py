import json

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
    "elapsed": "NOT_COMPUTED_B1",
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
elapsed=NOT_COMPUTED_B1
"""


def test_json_remains_the_default_shape_without_renderer_inference() -> None:
    output = render(REPORT, "json")

    assert json.loads(output) == REPORT
    assert "unobserved" not in output and "critical_path" not in output
