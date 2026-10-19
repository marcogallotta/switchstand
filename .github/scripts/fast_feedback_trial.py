"""One branch-only advisory experiment; authoritative Quality remains independent."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from switchstand.affected_tests import plan_exact, validate_execution_tree

# Deliberately representative, including real PostgreSQL runtime behavior. This
# fixed set does not claim complete affected coverage, even when the planner falls back.
REPRESENTATIVE_TESTS = (
    "tests/test_affected_tests.py",
    "tests/test_test_metrics.py",
    "tests/test_pytest_shard.py",
    "tests/test_canonical_service_runtime.py",
)


def main() -> int:
    repo = Path.cwd()
    destination = Path(os.environ["RUNNER_TEMP"]) / "fast-feedback-trial"
    destination.mkdir()
    subject = os.environ["TRIAL_SUBJECT"]
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if actual != subject:
        raise RuntimeError("immutable trial subject does not match checked out HEAD")
    plan = validate_execution_tree(
        repo, plan_exact(repo, os.environ["TRIAL_BASE"], subject), subject
    )
    (destination / "plan.json").write_text(json.dumps(asdict(plan), indent=2) + "\n")
    report = {
        "authority": "ADVISORY_REPRESENTATIVE_ONLY",
        "qualification": "Full authoritative Quality remains required",
        "subject": subject,
        "base": plan.base,
        "selection": REPRESENTATIVE_TESTS,
        "coverage": "PARTIAL: no affected-coverage or qualification claim",
        "planner_mode": plan.mode,
        "planner_fallback_reasons": plan.fallback_reasons,
        "run": {key: os.getenv(key) for key in (
            "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_WORKFLOW_REF",
            "RUNNER_OS", "RUNNER_ARCH", "ImageOS", "ImageVersion",
            "TRIAL_CACHE_HIT", "TRIAL_CACHE_KEY", "TRIAL_PYTHON_CACHE_HIT",
        )},
        "python": sys.version,
        "uv": subprocess.check_output(["uv", "--version"], text=True).strip(),
        "packages": sorted(
            f'{distribution.metadata["Name"]}=={distribution.version}'
            for distribution in importlib.metadata.distributions()
        ),
        "inputs_sha256": {
            path: hashlib.sha256((repo / path).read_bytes()).hexdigest()
            for path in ("pyproject.toml", "uv.lock", ".github/workflows/fast-feedback-trial.yml")
        },
        "checks": {},
    }
    commands = {
        "ruff": [sys.executable, "-m", "ruff", "check", "--no-cache", "."],
        "pyright": [sys.executable, "-m", "pyright", "--pythonpath", sys.executable],
        "pytest": [
            sys.executable, "-m", "pytest", "-ra", "-p", "no:cacheprovider",
            f"--junitxml={destination / 'junit.xml'}", "--durations=10",
            *REPRESENTATIVE_TESTS,
        ],
    }
    checks = {}
    status = 0
    for name, command in commands.items():
        print(f"==> Advisory {name}", flush=True)
        started = time.monotonic()
        result = subprocess.run(command, check=False)
        checks[name] = {
            "result": "PASS" if result.returncode == 0 else "FAIL",
            "exit_code": result.returncode,
            "seconds": round(time.monotonic() - started, 3),
        }
        status = status or result.returncode
    report["checks"] = checks
    rendered = json.dumps(report, indent=2, sort_keys=True)
    (destination / "results.json").write_text(rendered + "\n")
    print(rendered)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write("## Advisory representative trial\n\n")
        summary.write("Partial coverage only. **Full authoritative Quality remains required.**\n\n")
        summary.write(f"Subject: `{subject}`; planner: `{plan.mode}`.\n\n")
        summary.write("| Check | Result | Seconds |\n| --- | --- | --- |\n")
        for name, check in checks.items():
            summary.write(f"| {name} | {check['result']} | {check['seconds']} |\n")
        summary.write("\nSelected representative modules:\n\n")
        summary.writelines(f"- `{test}`\n" for test in REPRESENTATIVE_TESTS)
        summary.write(
            "\nPlan, fallback reasons, cache/environment identity and JUnit "
            "are retained as artifacts.\n"
        )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
