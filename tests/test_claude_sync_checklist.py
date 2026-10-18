import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/claude-sync-checklist"


def run(tmp_path: Path, priorities: str, jobs: str, *extra: str):
    priority_file = tmp_path / "PRIORITIES.md"
    jobs_file = tmp_path / "JOBS.md"
    priority_file.write_text(priorities)
    jobs_file.write_text(jobs)
    clock = "13:55" if "## RUNNING" not in priorities else "08:00"
    stamp = datetime.fromisoformat(f"2026-10-18T{clock}:00+02:00").timestamp()
    os.utime(priority_file, (stamp, stamp))
    command = [
        sys.executable,
        str(SCRIPT),
        "--priorities",
        str(priority_file),
        "--jobs",
        str(jobs_file),
        "--state-dir",
        str(tmp_path / "state"),
        "--plan-script",
        str(ROOT / "scripts/claude-plan"),
        "--now",
        "2026-10-18T14:00:00+02:00",
        *extra,
    ]
    return subprocess.run(command, text=True, capture_output=True, check=False), tmp_path / "state"


def test_scan_lists_every_open_item_and_flags_staleness_and_job_drift(tmp_path: Path) -> None:
    result, state = run(
        tmp_path,
        """# PRIORITIES
## RUNNING
- live: started 10-18 09:00; doing work
## QUEUE
H1. old: queued since 10-17; run next
H2. unknown: queued; missing timestamp
## DONE
closed
## FILES
references
""",
        """Stages: RUNNING -> DONE
| Job | Thread | Worktree | Stage | Next |
|---|---|---|---|---|
| live | t | wt | RUNNING since 09:00 10-18 | finish |
| dead | t | wt | RUNNING since 08:00 10-18 | finish |
| repeated | t | wt | RUNNING since 10-17 | old |
| repeated | t | wt | DONE 10-18 | - |
| unfinished | t | wt | DONE 10-17 | not deployed |
""",
        "--live-unit",
        "ss-codex-live-123456.scope",
        "--live-unit",
        "ss-codex-orphan-123456.scope",
    )
    alert = (state / "SYNC-ALERT.md").read_text()
    assert result.returncode == 0 and "## Every open item" in result.stdout
    for expected in (
        "PRIORITIES_STALE",
        "STALE_NO_JOB: priority:",
        "AGE_UNKNOWN: 1 open items",
        "DEAD_JOB: job:dead",
        "JOBS_DRIFT: repeated has 2 rows",
        "UNTRACKED_JOB: orphan",
        "job:unfinished",
    ):
        assert expected in alert
    assert "job:repeated" not in alert


def test_clean_scan_and_mailbox_due_ack_share_one_alert(tmp_path: Path) -> None:
    priorities = "# PRIORITIES\n## DONE\nclosed\n## FILES\nreferences\n"
    jobs = "Stages: RUNNING -> DONE\n"
    result, state = run(tmp_path, priorities, jobs, "--live-unit", "none")
    assert result.returncode == 0 and "- none" in result.stdout
    assert not (state / "SYNC-ALERT.md").exists()
    run(tmp_path, priorities, jobs, "--mailbox-due")
    assert "MAILBOX CHECK DUE" in (state / "SYNC-ALERT.md").read_text()
    shown = run(tmp_path, priorities, jobs, "--live-unit", "none")[0].stdout
    assert shown.startswith("# MAILBOX CHECK DUE")
    run(tmp_path, priorities, jobs, "--ack-mailbox")
    assert not (state / "SYNC-ALERT.md").exists()
    result = run(tmp_path, priorities + "## SURPRISE\nwork\n", jobs, "--live-unit", "none")[0]
    assert "SCAN_UNKNOWN: unclassified PRIORITIES.md section: SURPRISE" in result.stdout
