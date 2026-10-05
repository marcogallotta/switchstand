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


def test_head_mismatch_makes_source_unknown_without_a_span() -> None:
    result = add_github_evidence(BASE, qualification(head="c" * 40), HEAD)

    assert result["github"]["status"] == "UNKNOWN"
    assert result["github"]["reason"] == "EXPECTED_HEAD_MISMATCH"
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
