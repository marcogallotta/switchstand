import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/claude-sync-checklist"
REMINDER = ROOT / "scripts/claude-sync-reminder"


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
        "ss-codex-heavy-live-123456.scope",
        "--live-unit",
        "ss-codex-light-orphan-123456.scope",
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
    assert '"decision": "block"' in reminder(tmp_path / "home", alert).stdout


def test_clean_scan_and_mailbox_due_ack_share_one_alert(tmp_path: Path) -> None:
    priorities = "# PRIORITIES\n## DONE\nclosed\n## FILES\nreferences\n"
    jobs = "Stages: RUNNING -> DONE\n"
    result, state = run(tmp_path, priorities, jobs, "--live-unit", "none")
    assert result.returncode == 0 and "- none" in result.stdout
    assert not (state / "SYNC-ALERT.md").exists()
    run(tmp_path, priorities, jobs, "--mailbox-due")
    alert = (state / "SYNC-ALERT.md").read_text()
    assert "MAILBOX CHECK DUE" in alert
    assert '"decision": "block"' in reminder(tmp_path / "home", alert).stdout
    shown = run(tmp_path, priorities, jobs, "--live-unit", "none")[0].stdout
    assert shown.startswith("# MAILBOX CHECK DUE")
    run(tmp_path, priorities, jobs, "--ack-mailbox")
    assert not (state / "SYNC-ALERT.md").exists()
    result = run(tmp_path, priorities + "## SURPRISE\nwork\n", jobs, "--live-unit", "none")[0]
    assert "SCAN_UNKNOWN: unclassified PRIORITIES.md section: SURPRISE" in result.stdout


def reminder(tmp_path: Path, alert: str) -> subprocess.CompletedProcess[str]:
    state = tmp_path / ".local/state/switchstand/independent-audit-claude-20261008"
    state.mkdir(parents=True)
    (state / "SYNC-ALERT.md").write_text(alert)
    env = {**os.environ, "HOME": str(tmp_path), "SWITCHSTAND_SYNC_REMINDER": "1"}
    return subprocess.run(
        [sys.executable, str(REMINDER)],
        input="{}",
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_waiting_marco_and_unknown_age_are_info_only_with_pick_up(tmp_path: Path) -> None:
    priorities = """# PRIORITIES
## PICK UP HERE
- E2E: WAITING-MARCO; continue when Marco returns
## QUEUE
- mystery: no timestamp
## DONE
closed
## FILES
references
"""
    jobs = """Stages: RUNNING -> DONE
| E2E testing | thread | WAITING-MARCO 10-16 | Marco designs live |
"""
    _, state = run(tmp_path, priorities, jobs, "--live-unit", "none")
    alert = (state / "SYNC-ALERT.md").read_text()
    assert alert.count("WAITING_MARCO:") == 1
    assert "AGE_UNKNOWN:" in alert
    assert "## Blocking" not in alert
    assert reminder(tmp_path / "home", alert).stdout == ""


def test_waiting_marco_without_pick_up_blocks(tmp_path: Path) -> None:
    priorities = "# PRIORITIES\n## PICK UP HERE\n- E2E: resume later\n## DONE\nclosed\n"
    jobs = """Stages: RUNNING -> DONE
| Review instruction bugs | thread | WAITING-MARCO 10-17 | Marco pastes replies |
"""
    _, state = run(tmp_path, priorities, jobs, "--live-unit", "none")
    alert = (state / "SYNC-ALERT.md").read_text()
    assert alert.count("WAITING_MARCO:") == 1
    assert "WAITING_MARCO_NO_PICK_UP:" in alert
    assert '"decision": "block"' in reminder(tmp_path / "home", alert).stdout


def test_capacity_counts_classified_units_and_flags_only_excess(tmp_path: Path) -> None:
    priorities = "# PRIORITIES\n## DONE\nclosed\n## FILES\nreferences\n"
    jobs = "Stages: RUNNING -> DONE\n"
    units = [
        *(f"ss-codex-heavy-h{number}-123456.scope" for number in range(1, 6)),
        *(f"ss-codex-light-l{number}-123456.scope" for number in range(1, 3)),
    ]
    result, state = run(
        tmp_path,
        priorities,
        jobs,
        *(argument for unit in units for argument in ("--live-unit", unit)),
    )
    alert = (state / "SYNC-ALERT.md").read_text()
    assert "- heavy 5/4 | total 7/6" in result.stdout
    assert "HEAVY_CAPACITY_EXCEEDED: 5 running; max 4" in alert
    assert "TOTAL_CAPACITY_EXCEEDED: 7 running; max 6" in alert

    result, state = run(
        tmp_path,
        priorities,
        jobs,
        *(argument for unit in units[:4] + units[5:] for argument in ("--live-unit", unit)),
    )
    assert "- heavy 4/4 | total 6/6" in result.stdout
    alert = (state / "SYNC-ALERT.md").read_text()
    assert "HEAVY_CAPACITY_EXCEEDED" not in alert
    assert "TOTAL_CAPACITY_EXCEEDED" not in alert


def test_unclassified_live_unit_is_a_blocking_identity_error(tmp_path: Path) -> None:
    priorities = "# PRIORITIES\n## DONE\nclosed\n## FILES\nreferences\n"
    result, state = run(
        tmp_path,
        priorities,
        "Stages: RUNNING -> DONE\n",
        "--live-unit",
        "ss-codex-mailbox-123456.scope",
    )
    assert "- heavy 0/4 | total 1/6" in result.stdout
    assert (
        "JOB_CLASS_UNKNOWN: ss-codex-mailbox-123456.scope must identify heavy or light"
        in (state / "SYNC-ALERT.md").read_text()
    )
