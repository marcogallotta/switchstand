"""Exercise the real CLI with real Git and harmless subprocesses."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).parents[1] / "scripts/spec-test-commands"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "candidate"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    (repo / "tracked").write_text("original")
    git(repo, "add", "tracked")
    git(repo, "commit", "-m", "fixture")
    return repo, tmp_path / "spec.json", git(repo, "rev-parse", "HEAD")


def check(code="print('actual required check')", **changes):
    return {"name": "required", "argv": [sys.executable, "-c", code], "cwd": ".",
                "timeout_seconds": 3, "noninteractive": True} | changes


def spec(path, checks):
    source = {"status": "ok", "item": {"id": "work-123", "revision": "pg_exact",
                  "notes": "Implementation Specification\n```spec-test-commands\n"
                  + json.dumps(checks) + "\n```\n"}}
    path.write_text(json.dumps(source))
    return source


def run(setup, *extra):
    repo, path, head = setup
    result = subprocess.run(
        [sys.executable, RUNNER, "--spec", path, "--repo", repo,
         "--work-id", "work-123", "--revision", "pg_exact", "--candidate", head, *extra],
        text=True, capture_output=True, timeout=15, check=False,
        env=os.environ | {"DISPLAY": ":fake", "WAYLAND_DISPLAY": "fake", "BROWSER": "firefox"},
    )
    return result.returncode, json.loads(result.stdout)


def test_reads_every_required_command_from_full_work_export(setup):
    repo, path, head = setup
    (repo / "sub").mkdir()  # Empty directories do not dirty Git.
    source = spec(path, [check("print('FIRST')"),
                        check("import os; print('SECOND', os.path.basename(os.getcwd()))",
                              name="second", cwd="sub")])
    path.write_text(json.dumps({"isError": False, "structuredContent": source}))
    code, result = run(setup)
    assert code == 0 and result["status"] == "PASS"
    assert [c["stdout"].strip() for c in result["checks"]] == ["FIRST", "SECOND sub"]
    assert result["spec_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (result["candidate"], result["spec_work_id"], result["spec_revision"]) == (
        head, "work-123", "pg_exact")
    assert all(c["exit_code"] == 0 and c["duration_seconds"] > 0 for c in result["checks"])


def test_failure_preserves_output_exit_and_still_runs_next_required(setup):
    _, path, _ = setup
    spec(path, [check("import sys; print('reason', file=sys.stderr); sys.exit(7)"),
                check(name="next")])
    code, result = run(setup)
    assert code != 0 and result["status"] == "FAIL"
    assert result["checks"][0]["exit_code"] == 7
    assert result["checks"][0]["stderr"] == "reason\n"
    assert result["checks"][1]["status"] == "PASS"
    assert result["next_owner"] == "work-123" and "Diagnose" in result["next_action"]


@pytest.mark.parametrize("checks", [[], [check(argv=[])], [check(noninteractive=False)],
                                   [check(cwd="..")], [check(timeout_seconds=0)],
                                   [check(), check()], [check(skip=True)]])
def test_invalid_or_missing_required_checks_never_pass(setup, checks):
    spec(setup[1], checks)
    code, result = run(setup)
    assert code != 0 and result["status"] == "NOT_RUN" and result["checks"] == []


@pytest.mark.parametrize("kind", ["missing", "invalid", "no_block", "error", "wrong_work",
                                  "wrong_revision", "failed_read"])
def test_invalid_source_never_executes(setup, kind):
    path = setup[1]
    source = spec(path, [check()])
    if kind == "missing":
        path.unlink()
    elif kind == "invalid":
        path.write_text("not json")
    else:
        if kind == "no_block":
            source["item"]["notes"] = "No commands"
        elif kind == "wrong_work":
            source["item"]["id"] = "other"
        elif kind == "wrong_revision":
            source["item"]["revision"] = "old"
        elif kind == "failed_read":
            source["status"] = "unknown"
        else:
            source = {"isError": True, "structuredContent": source}
        path.write_text(json.dumps(source))
    code, result = run(setup)
    assert code != 0 and result["status"] == "NOT_RUN" and not result["checks"]


def test_missing_executable_does_not_skip_to_pass(setup):
    spec(setup[1], [check(argv=["/does/not/exist"]), check(name="next")])
    code, result = run(setup)
    assert code != 0 and result["status"] == "NOT_RUN"
    assert [c["status"] for c in result["checks"]] == ["NOT_RUN", "PASS"]
    assert result["checks"][0]["exit_code"] is None


def test_closed_stdin_and_display_guards_are_real(setup):
    spec(setup[1], [check("import os, sys; assert sys.stdin.read() == ''; "
                         "assert 'DISPLAY' not in os.environ; "
                         "assert 'WAYLAND_DISPLAY' not in os.environ; "
                         "assert os.environ['BROWSER'] == 'false'; "
                         "assert os.environ['CI'] == '1'")])
    assert run(setup)[1]["status"] == "PASS"
    spec(setup[1], [check("input('prompt')")])
    assert run(setup)[1]["status"] == "FAIL"
    spec(setup[1], [check("import time; time.sleep(60)", timeout_seconds=0.1)])
    _, result = run(setup)
    assert result["status"] == "FAIL" and result["checks"][0]["reason"] == "timeout"


@pytest.mark.parametrize("drift", ["source", "head", "tree", "initial_head", "initial_tree"])
def test_candidate_and_source_drift_never_pass(setup, drift):
    repo, path, _ = setup
    code = "print('ok')"
    if drift == "source":
        code = f"from pathlib import Path; Path({str(path)!r}).write_text('changed')"
    elif drift == "head":
        code = "import subprocess; subprocess.run(['git','commit','--allow-empty','-m','drift'],check=True)"
    elif drift == "tree":
        code = "from pathlib import Path; Path('tracked').write_text('drift')"
    elif drift == "initial_head":
        git(repo, "commit", "--allow-empty", "-m", "drift")
    elif drift == "initial_tree":
        (repo / "tracked").write_text("drift")
    spec(path, [check(code)])
    status, result = run(setup)
    assert status != 0 and result["status"] == (
        "NOT_RUN" if drift.startswith("initial") else "STALE")
