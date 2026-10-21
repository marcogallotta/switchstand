import json
import sys
from typing import Literal
from uuid import UUID

import pytest

from switchstand import flow_report
from switchstand.flow_report import add_github_evidence, project_wall
from switchstand.repository_candidate import QualificationGate, RepositoryCandidateQualification

HEAD = "a" * 40
COMPOSITION = "b" * 40
BASE: dict[str, object] = {"status": "PARTIAL", "coverage": {"github": {
    "status": "EXCLUDED", "reason": "NOT_INCLUDED_B1",
}}}


def qualification(
    *gates: QualificationGate, status: Literal["READY", "NOT_READY", "UNKNOWN"] = "READY",
    head: str = HEAD,
) -> RepositoryCandidateQualification:
    return RepositoryCandidateQualification(
        status=status, pull_request=17, head_sha=head, composition_sha=COMPOSITION,
        gates=list(gates),
    )


def gate(
    name: str, kind: Literal["exact_head", "composition"],
    start: str | None, end: str | None,
) -> QualificationGate:
    return QualificationGate(
        name=name, subject_kind=kind, subject_sha=HEAD if kind == "exact_head" else COMPOSITION,
        state="completed", conclusion="success", started_at=start, completed_at=end,
    )


def test_caller_supplied_exact_subjects_union_overlaps_without_cross_subject_sum() -> None:
    result = add_github_evidence(BASE, qualification(
        gate("head-1", "exact_head", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:10+00:00"),
        gate("head-2", "exact_head", "2026-01-01T00:00:05+00:00",
             "2026-01-01T00:00:15+00:00"),
        gate("composition", "composition", "2026-01-01T00:00:07+00:00",
             "2026-01-01T00:00:12+00:00"),
    ), HEAD)

    assert result["github"]["correlation"] == "CALLER_SUPPLIED"
    assert result["github"]["subjects"]["exact_head"]["union_ms"] == 15_000
    assert result["github"]["subjects"]["composition"]["union_ms"] == 5_000
    assert "union_ms" not in result["github"]
    assert result["coverage"]["github"] == {
        "status": "INCLUDED", "reason": "CALLER_SUPPLIED",
    }
    assert result["source_coverage"]["github_ci"] == {
        "status": "KNOWN", "reason": "CALLER_SUPPLIED",
    }


def test_wall_projection_unions_github_subjects_instead_of_summing_them() -> None:
    value: dict[str, object] = {
        **BASE,
        "captured_at": "2026-01-01T00:00:20+00:00",
        "admission": {"status": "KNOWN", "at": "2026-01-01T00:00:00+00:00"},
    }
    result = add_github_evidence(value, qualification(
        gate("head", "exact_head", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:10+00:00"),
        gate("composition", "composition", "2026-01-01T00:00:05+00:00",
             "2026-01-01T00:00:15+00:00"),
    ), HEAD)

    assert result["elapsed"]["observed_interval_union_ms"] == 15_000
    assert result["elapsed"]["unobserved_wall_ms"] == 5_000


def test_unsafe_interval_makes_union_and_remainder_unknown() -> None:
    result = project_wall({
        "captured_at": "2026-01-01T00:00:20+00:00",
        "admission": {"status": "KNOWN", "at": "2026-01-01T00:00:00+00:00"},
        "human_review": {"items": [{"wait": {
            "start": "2026-01-01T00:00:01", "end": "2026-01-01T00:00:02+00:00",
        }}]},
    })

    assert result["elapsed"]["wall_status"] == "KNOWN"
    assert result["elapsed"]["projection_status"] == "UNKNOWN"
    assert result["elapsed"]["projection_reason"] == "UNSAFE_INTERVAL"
    assert result["elapsed"]["observed_interval_union_ms"] is None
    assert result["elapsed"]["unobserved_wall_ms"] is None
    assert result["delivery_timing"]["gaps"] is None


def test_delivery_timing_preserves_overlap_source_identity_and_exact_complement() -> None:
    value = {
        "captured_at": "2026-01-01T01:00:00+00:00",
        "admission": {"status": "KNOWN", "at": "2026-01-01T00:00:00+00:00"},
        "review_pickup": {"status": "KNOWN", "review_id": "review-exact",
                          "requested_at": "2026-01-01T00:00:00+00:00",
                          "received_at": "2026-01-01T00:20:00+00:00",
                          "verdict_at": "2026-01-01T00:20:00+00:00"},
        "run_receipts": {"status": "PARTIAL", "truncated": True, "attempts": [{
            "request_id": "research-request", "task_kind": "RESEARCH",
            "requested_at": "2026-01-01T00:10:00+00:00",
            "bound_at": "2026-01-01T00:10:00+00:00",
            "completed_at": "2026-01-01T00:40:00+00:00",
        }]},
    }
    result = project_wall(value)
    timing = result["delivery_timing"]
    assert timing["status"] == "PARTIAL"
    assert timing["interval_cost_sum_ms"] == 50 * 60_000
    assert result["elapsed"]["observed_interval_union_ms"] == 40 * 60_000
    assert timing["gaps"] == [{"start": "2026-01-01T00:40:00+00:00",
                               "end": value["captured_at"], "duration_ms": 20 * 60_000}]
    assert timing["largest_gap"] == timing["gaps"][0]
    assert timing["large_gaps"] == []
    assert [(row["source_id"], row["kind"]) for row in timing["intervals"]] == [
        ("review-exact", "REVIEW_REQUEST_TO_RECEIVE"),
        ("research-request", "BOUND_RUN_ENVELOPE"),
    ]
    assert timing["largest_interval"]["source_id"] == "research-request"
    assert result["run_receipts"]["truncated"] is True
    assert "NOT_ACTIVE_EFFORT_OR_CAUSE" in timing["interpretation"]


def test_delivery_timing_orders_clipped_gaps_and_keeps_failure_spans_distinct() -> None:
    result = project_wall({
        "captured_at": "2026-01-01T03:00:00+00:00",
        "admission": {"status": "KNOWN", "at": "2026-01-01T00:00:00+00:00"},
        "failures": {"status": "KNOWN", "items": [
            {"attempt_id": "later", "occurred_at": "2026-01-01T02:00:00+00:00",
             "resolved_at": "2026-01-01T02:30:00+00:00"},
            {"attempt_id": "first", "occurred_at": "2025-12-31T23:30:00+00:00",
             "resolved_at": "2026-01-01T00:30:00+00:00"},
            {"attempt_id": "unresolved", "occurred_at": "2026-01-01T00:30:00+00:00",
             "resolved_at": None},
        ]},
    })
    timing = result["delivery_timing"]
    assert [row["source_id"] for row in timing["intervals"]] == ["first", "later"]
    assert all(row["kind"] == "RECORDED_FAILURE_OPEN" for row in timing["intervals"])
    assert [row["duration_ms"] for row in timing["large_gaps"]] == [90 * 60_000, 30 * 60_000]
    assert sum(row["duration_ms"] for row in timing["gaps"]) == 120 * 60_000
    assert result["elapsed"]["unobserved_wall_ms"] == 120 * 60_000


def test_delivery_timing_unbound_request_does_not_cover_unexplained_wall() -> None:
    result = project_wall({
        "captured_at": "2026-01-01T01:00:00+00:00",
        "admission": {"status": "KNOWN", "at": "2026-01-01T00:00:00+00:00"},
        "run_receipts": {"status": "KNOWN", "attempts": [{
            "request_id": "open", "requested_at": "2026-01-01T00:30:00+00:00",
            "bound_at": None, "completed_at": None,
        }]},
    })
    assert result["delivery_timing"]["intervals"] == []
    assert result["delivery_timing"]["largest_gap"]["duration_ms"] == 60 * 60_000
    assert result["run_receipts"]["attempts"][0]["request_id"] == "open"


@pytest.mark.parametrize("admission", [None, "2026-01-02T00:00:00+00:00"])
def test_delivery_timing_cannot_locate_gaps_without_safe_window(admission: str | None) -> None:
    result = project_wall({
        "captured_at": "2026-01-01T01:00:00+00:00",
        "admission": {"status": "KNOWN", "at": admission},
        "run_receipts": {"status": "KNOWN", "attempts": [{
            "request_id": "research", "requested_at": "2026-01-01T00:30:00+00:00",
            "bound_at": "2026-01-01T00:40:00+00:00",
            "completed_at": "2026-01-01T00:50:00+00:00",
        }]},
    })
    assert result["delivery_timing"]["status"] == "UNKNOWN"
    assert result["delivery_timing"]["gaps"] is None
    assert result["delivery_timing"]["large_gaps"] is None
    assert len(result["delivery_timing"]["intervals"]) == 2
    assert all(row["window_basis"] == "SOURCE_INTERVAL_ONLY"
               for row in result["delivery_timing"]["intervals"])


def test_head_mismatch_makes_source_unknown_without_a_span() -> None:
    result = add_github_evidence(BASE, qualification(head="c" * 40), HEAD)

    assert result["github"]["status"] == "UNKNOWN"
    assert result["github"]["reason"] == "EXPECTED_HEAD_MISMATCH"
    assert result["source_coverage"]["github_ci"] == {
        "status": "UNKNOWN", "reason": "EXPECTED_HEAD_MISMATCH",
    }
    assert all(subject["intervals"] == [] and subject["union_ms"] is None
               for subject in result["github"]["subjects"].values())


def test_incomplete_timestamp_makes_source_unknown_without_fabricated_interval() -> None:
    result = add_github_evidence(BASE, qualification(
        gate("head", "exact_head", "2026-01-01T00:00:00+00:00", None),
        gate("composition", "composition", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:01+00:00"),
    ), HEAD)

    assert result["github"]["status"] == "UNKNOWN"
    assert result["github"]["subjects"]["exact_head"] == {
        "status": "UNKNOWN", "reason": "INCOMPLETE_GATE_TIMESTAMPS",
        "subject_sha": HEAD, "intervals": [], "union_ms": None,
    }
    assert result["github"]["subjects"]["composition"]["status"] == "OBSERVED"


def test_provider_failure_is_unknown_and_does_not_use_gate_data() -> None:
    result = add_github_evidence(BASE, qualification(status="UNKNOWN"), HEAD)

    assert result["coverage"]["github"] == {
        "status": "UNKNOWN", "reason": "PROVIDER_UNAVAILABLE",
    }
    assert result["github"]["subjects"]["exact_head"]["intervals"] == []


async def test_run_qualifies_only_the_explicit_caller_supplied_pr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    class Engine:
        async def dispose(self) -> None:
            pass

    seen: list[int] = []

    async def fake_report(engine: object, work_id: UUID) -> dict[str, object]:
        del engine, work_id
        return BASE

    async def fake_qualify(pull_request: int) -> RepositoryCandidateQualification:
        seen.append(pull_request)
        return qualification(head="c" * 40).model_copy(update={"pull_request": pull_request})

    monkeypatch.setattr(flow_report, "create_async_engine", lambda url: Engine())
    monkeypatch.setattr(flow_report, "report", fake_report)
    monkeypatch.setattr(flow_report, "qualify_repository_candidate", fake_qualify)
    monkeypatch.setenv("DATABASE_URL", "not-opened")

    await flow_report._run(
        UUID("10000000-0000-4000-8000-000000000001"),
        "json", 27, HEAD,
    )

    assert seen == [27]
    output = json.loads(capsys.readouterr().out)
    assert output["github"]["correlation"] == "CALLER_SUPPLIED"
    assert output["github"]["pull_request"] == 27


@pytest.mark.parametrize("github_argument", [("--github-pr", "27"),
                                              ("--github-head-sha", HEAD)])
def test_cli_rejects_partial_github_identity(
    monkeypatch: pytest.MonkeyPatch, github_argument: tuple[str, str],
) -> None:
    monkeypatch.setattr(sys, "argv", [
        "switchstand-flow-report", "--work-id",
        "10000000-0000-4000-8000-000000000001", *github_argument,
    ])

    with pytest.raises(SystemExit, match="2"):
        flow_report.main()
