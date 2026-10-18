import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "claude-plan"


def fixtures(tmp_path: Path) -> tuple[Path, Path]:
    priorities = tmp_path / "PRIORITIES.md"
    priorities.write_text(
        """# PRIORITIES — REWRITE 2026-10-18 10:00
context
## RUNNING
- livejob: running
## QUEUE
H1. Tracker build: NOT sent; Human Review owed.
## NOT IN PLAN (hard reason)
Design sessions: human is away; cleanup is owned elsewhere.
## STALLED
Review fix since 10-16; includes bugs 8-12 and 1-2 CLAUDE.md lines.
## HR FORMAT RESULT (hrloop2 DONE 10:4x) — show Marco
Recommendation awaiting Marco.
## OWNERS
non-work reference
## DONE
closed work
## FILES
references
"""
    )
    jobs = tmp_path / "JOBS.md"
    jobs.write_text(
        """# jobs
Stages: RUNNING -> DONE
| Job | Thread | Worktree | Stage | Next |
|---|---|---|---|---|
| livejob | t | wt | RUNNING since 09:00 10-18 | finish report |
| closed | t | wt | DONE 10-16 | - |
| held | t | wt | STOPPED 10-17 | pending next action |
Known issue: Fix launch flags before next job.
## SESSIONS
Priority: E2E testing missing, position TBD.
- rca-1016: next: read report
"""
    )
    stamp = datetime(2026, 10, 18, 10, 0, tzinfo=UTC).timestamp()
    os.utime(priorities, (stamp, stamp))
    os.utime(jobs, (stamp, stamp))
    return priorities, jobs


def run(
    priorities: Path,
    jobs: Path,
    decisions: Path | None = None,
    now: str = "2026-10-18T12:00:00+00:00",
):
    command = [
        sys.executable,
        str(SCRIPT),
        "--priorities",
        str(priorities),
        "--jobs",
        str(jobs),
        "--now",
        now,
    ]
    if decisions:
        command += ["--decisions", str(decisions)]
    return subprocess.run(command, text=True, capture_output=True, check=False)


def complete(template: str) -> str:
    result = template.replace(
        "\tOUT\t<hard reason; or PLAN with finish condition>",
        "\tOUT\toutside this wave: owner or dependency controls it",
    )
    return result.replace(
        "job:livejob:5\tOUT\toutside this wave: owner or dependency controls it",
        "job:livejob:5\tPLAN\tfinish report, then start Tracker build",
    )


def test_real_shapes_are_records_without_semicolon_splitting(tmp_path):
    priorities, jobs = fixtures(tmp_path)
    result = run(priorities, jobs)

    assert result.returncode == 2
    assert result.stdout.count("priority:6\t") == 1
    assert "job:Known issue:8" in result.stdout
    assert "job:Priority:10" in result.stdout
    assert "job:rca-1016:11" in result.stdout
    assert "job:closed:6" in result.stdout  # superset avoids false open/closed claims


def test_report_is_short_first_complete_below_and_ages_only_contextual_dates(tmp_path):
    priorities, jobs = fixtures(tmp_path)
    decisions = tmp_path / "decisions.tsv"
    decisions.write_text(complete(run(priorities, jobs).stdout))

    result = run(priorities, jobs, decisions)

    assert result.returncode == 0
    assert "## NOW" in result.stdout
    assert "finish report, then start Tracker build" in result.stdout
    assert "## COMPLETE SOURCE INVENTORY" in result.stdout
    assert "Review fix since 10-16; includes bugs 8-12 and 1-2 CLAUDE.md lines." in result.stdout
    assert "[≥1d] Review fix since 10-16" in result.stdout
    assert "job:held:7` STOPPED 10-17 — pending next action | ≥12h | STALLED/HELD" in result.stdout
    assert "STALLED/HELD" in result.stdout
    assert "289d" not in result.stdout
    assert "67d" not in result.stdout


def test_stale_or_incomplete_decisions_fail_closed(tmp_path):
    priorities, jobs = fixtures(tmp_path)
    template = run(priorities, jobs).stdout
    decisions = tmp_path / "decisions.tsv"
    decisions.write_text(template)
    assert "hard reason required" in run(priorities, jobs, decisions).stdout

    for disposition in ("PLAN", "OUT"):
        decisions.write_text(
            complete(template).replace(
                "priority:4\tOUT\toutside this wave: owner or dependency controls it",
                f"priority:4\t{disposition}\t   ",
            )
        )
        assert "hard reason required" in run(priorities, jobs, decisions).stdout

    decisions.write_text(complete(template))
    jobs.write_text(jobs.read_text() + "new obligation\n")
    assert "stale source hashes" in run(priorities, jobs, decisions).stdout


def test_unknown_priorities_section_fails_closed(tmp_path):
    priorities, jobs = fixtures(tmp_path)
    base = priorities.read_text()
    for heading in ("SURPRISE", "RUNNINGLY", "DONE-ish"):
        priorities.write_text(base + f"## {heading}\nwork\n")
        result = run(priorities, jobs)
        assert result.returncode == 2
        assert f"unclassified PRIORITIES.md section: {heading}" in result.stdout


def test_date_only_bound_and_year_rollover_are_conservative(tmp_path):
    priorities, jobs = fixtures(tmp_path)
    jobs.write_text(jobs.read_text() + "| yearend | t | wt | FAILED 12-31 23:00 | finish |\n")
    now = "2027-01-01T01:00:00+00:00"
    decisions = tmp_path / "decisions.tsv"
    decisions.write_text(complete(run(priorities, jobs, now=now).stdout))
    result = run(priorities, jobs, decisions, now)
    assert "job:yearend:12` FAILED 12-31 23 | 2h" in result.stdout
