import json
import os
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/switchstand-e2e-qualify"


def subject():
    return runpy.run_path(str(SCRIPT), run_name="switchstand_e2e_qualify_test")


@pytest.mark.parametrize("label", ["S1", "S3"])
def test_required_scenario_skip_is_not_run(tmp_path, monkeypatch, label):
    namespace = subject()

    def run(command, **_kwargs):
        report = Path(next(value.removeprefix("--junitxml=") for value in command
                           if value.startswith("--junitxml=")))
        report.write_text(
            '<testsuite tests="1" skipped="1" failures="0">'
            '<testcase><skipped /></testcase></testsuite>'
        )
        return SimpleNamespace(returncode=0, stdout="Disposable PostgreSQL teardown: ok\n")

    monkeypatch.setattr(namespace["subprocess"], "run", run)
    status, executed, skipped, teardown = namespace["scenario"](
        label, "tests/test_example.py::test_example", None, Path("python"), tmp_path,
    )

    assert (status, executed, skipped, teardown) == ("NOT_RUN", 0, 1, True)


def git(repo, *arguments):
    return subprocess.run(
        ["git", "-C", str(repo), *arguments], check=True, text=True,
        capture_output=True,
    ).stdout.strip()


def test_migrations_are_compared_with_exact_release_baseline(tmp_path):
    namespace = subject()
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README").write_text("baseline\n")
    git(tmp_path, "add", "README")
    git(tmp_path, "commit", "-qm", "baseline")
    baseline = git(tmp_path, "rev-parse", "HEAD")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001.py").write_text("revision = '0001'\n")
    git(tmp_path, "add", "migrations/0001.py")
    git(tmp_path, "commit", "-qm", "migration")
    candidate = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", candidate)
    namespace["release_baseline"].__globals__["ROOT"] = tmp_path

    assert namespace["release_baseline"](baseline, candidate) == baseline
    assert namespace["migration_status"](baseline, candidate) == "HOLD"


def test_release_baseline_must_strictly_precede_candidate(tmp_path):
    namespace = subject()
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README").write_text("candidate\n")
    git(tmp_path, "add", "README")
    git(tmp_path, "commit", "-qm", "candidate")
    candidate = git(tmp_path, "rev-parse", "HEAD")
    namespace["release_baseline"].__globals__["ROOT"] = tmp_path

    with pytest.raises(ValueError, match="must precede"):
        namespace["release_baseline"](candidate, candidate)


def test_dirty_execution_tree_cannot_represent_candidate(tmp_path):
    namespace = subject()
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    tracked = tmp_path / "qualified.py"
    tracked.write_text("EXPECTED = True\n")
    git(tmp_path, "add", "qualified.py")
    git(tmp_path, "commit", "-qm", "candidate")
    candidate = git(tmp_path, "rev-parse", "HEAD")
    namespace["require_clean_candidate"].__globals__["ROOT"] = tmp_path
    namespace["require_clean_candidate"](candidate)

    tracked.write_text("EXPECTED = False\n")
    with pytest.raises(ValueError, match="not clean"):
        namespace["require_clean_candidate"](candidate)


def result():
    return {
        "run_id": "7b4ff19b-4332-4a04-8d47-188cc1d5e213",
        "work_id": "946b593d-1f2b-5442-96f8-cf1a12482351",
        "candidate_sha": "b" * 40,
        "release_baseline_sha": "a" * 40,
        "receipt_schema": 1,
        "suite_version": "v1-a.2",
        "started_at": "2026-10-18T00:00:00+00:00",
        "ended_at": "2026-10-18T00:01:00+00:00",
        "S1": "PASS", "S2": "PASS", "S3": "PASS", "M": "NOT_APPLICABLE",
        "executed": 3, "skipped": 0, "teardown_ok": True, "failure_reason": None,
    }


def test_receipt_is_sealed_and_verified_from_private_readback(tmp_path, monkeypatch):
    namespace = subject()
    monkeypatch.setenv("HOME", str(tmp_path))
    key_path = tmp_path / "receipt.key"
    key_path.write_bytes(os.urandom(32))
    key_path.chmod(0o600)
    key = namespace["receipt_key"](key_path)

    verified, digest = namespace["publish"](result(), key)
    receipt_path = tmp_path / ".local/state/switchstand/e2e-runs" / (
        result()["run_id"] + ".json"
    )

    assert verified == result()
    assert len(digest) == 64
    assert receipt_path.stat().st_mode & 0o777 == 0o600

    tampered = json.loads(receipt_path.read_text())
    tampered["S1"] = "FAIL"
    receipt_path.write_bytes(namespace["canonical"](tampered))
    receipt_path.chmod(0o600)
    with pytest.raises(ValueError, match="seal is invalid"):
        namespace["verify_receipt"](receipt_path, key)


def test_receipt_readback_must_match_completed_run(tmp_path, monkeypatch):
    namespace = subject()
    monkeypatch.setenv("HOME", str(tmp_path))
    key_path = tmp_path / "receipt.key"
    key_path.write_bytes(b"k" * 32)
    key_path.chmod(0o600)
    key = namespace["receipt_key"](key_path)
    namespace["publish"](result(), key)
    receipt_path = tmp_path / ".local/state/switchstand/e2e-runs" / (
        result()["run_id"] + ".json"
    )
    expected = {**result(), "candidate_sha": "c" * 40}

    with pytest.raises(ValueError, match="does not match"):
        namespace["verify_receipt"](receipt_path, key, expected)
