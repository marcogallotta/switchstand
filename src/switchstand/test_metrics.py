"""Non-authoritative, reusable JUnit timing records for GitHub artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from xml.etree import ElementTree

from switchstand.qualification_lineage import (
    ProviderAttemptIdentity,
    qualification_lineage_id,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def qualification_attempt_observation(
    identity: dict[str, Any], planner: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project existing GitHub, plan, harness, and timing evidence without authority."""

    raw_github = identity.get("github")
    github = cast(dict[str, Any], raw_github) if isinstance(raw_github, dict) else {}
    run_attempt = github.get("run_attempt")
    retry_reason = "INITIAL" if str(run_attempt) == "1" else "UNKNOWN"
    try:
        repository = github["repository"]
        workflow = github["workflow"]
        job = github["job"]
        run_id = github["run_id"]
        if not all(isinstance(value, str) for value in (repository, workflow, job)):
            raise TypeError("provider strings unavailable")
        if not isinstance(run_id, (str, int)) or not isinstance(run_attempt, (str, int)):
            raise TypeError("provider run identity unavailable")
        provider = ProviderAttemptIdentity(
            cast(str, repository), cast(str, workflow), cast(str, job),
            int(run_id), int(run_attempt),
        )
        provider_record: dict[str, Any] = {
            "repository": provider.repository, "workflow": provider.workflow,
            "job": provider.job, "run_id": provider.run_id,
            "run_attempt": provider.run_attempt,
            "executed_attempt_id": provider.executed_attempt_id,
        }
        lineage = qualification_lineage_id(
            provider.repository, "quality", "qualification-lineage-v1",
        )
    except (KeyError, TypeError, ValueError):
        provider_record = dict(github)
        lineage = None
    rework = identity.get("qualification_rework")
    if not isinstance(rework, dict):
        rework = {}
    rework = cast(dict[str, Any], rework)
    environment = identity.get("environment")
    if not isinstance(environment, dict):
        environment = {}
    environment = cast(dict[str, Any], environment)
    harness_parts: dict[str, object] = {
        "workflow_blob": environment.get("workflow_blob"),
        "manifest_blobs": environment.get("manifest_blobs"),
        "candidate_image": identity.get("candidate_image"),
    }
    return {
        "lineage_id": lineage,
        "provider": provider_record,
        "scope": {
            "candidate_sha": identity.get("subject_sha"),
            "base_sha": identity.get("base_sha"),
            "plan_digest": _digest(planner) if planner is not None else None,
            "harness_digest": (
                _digest(harness_parts) if all(harness_parts.values()) else None
            ),
            "environment_digest": identity.get("environment_key"),
        },
        "retry_reason": rework.get("retry_reason", retry_reason),
        "started_at": identity.get("started_at"),
        "completed_at": identity.get("completed_at"),
        "decisive_failure_at": (
            identity.get("completed_at") if identity.get("quality_outcome") == "failure" else None
        ),
        "rework": {
            "harness_seconds": rework.get("harness_seconds"),
            "wrapper_seconds": rework.get("wrapper_seconds"),
            "fixture_seconds": rework.get("fixture_seconds"),
            "setup_seconds": rework.get("setup_seconds"),
        },
        "current": identity.get("current"),
    }


def collect(junit: Path, identity: dict[str, Any], execution_kind: str,
            planner: dict[str, Any] | None = None) -> dict[str, Any]:
    record: dict[str, Any] = identity | {
        "schema_version": 1, "execution_kind": execution_kind,
        "collected_at": datetime.now(UTC).isoformat(),
        "selected_count": None, "total_test_count": None, "selection_ratio": None,
        "fallback_reasons": [],
    }
    if planner is not None:
        record["planner"] = {k: planner.get(k) for k in ("planner_revision", "base", "head", "mode")}
        record["selected_count"] = len(planner["selected_tests"])
        record["fallback_reasons"] = planner.get("fallback_reasons", [])
        # Planner selections are test files; JUnit counts are test cases. Never divide them.
        record["selection_unit"] = "test_file"
        record["total_test_count"] = planner.get("total_test_count")
        total = record["total_test_count"]
        if isinstance(total, int) and total > 0:
            record["selection_ratio"] = record["selected_count"] / total
    record["qualification_attempt"] = qualification_attempt_observation(identity, planner)
    try:
        root = ElementTree.parse(junit).getroot()
        if root.tag not in ("testsuite", "testsuites"):
            raise ValueError("not JUnit")
        cases = list(root.iter("testcase"))
        times = [float(case.get("time", "0")) for case in cases]
        if any(not math.isfinite(value) or value < 0 for value in times):
            raise ValueError("invalid duration")
        suite_times = [float(suite.get("time", "0")) for suite in root.iter("testsuite")]
        if any(not math.isfinite(value) or value < 0 for value in suite_times):
            raise ValueError("invalid suite duration")
        slow: list[dict[str, Any]] = [{"test": f"{case.get('classname', '')}::{case.get('name', '')}", "seconds": value}
                for case, value in zip(cases, times, strict=True)]
        record["pytest"] = {
            "test_count": len(cases),
            **{f"{tag}_count": sum(case.find(tag) is not None for case in cases)
               for tag in ("failure", "error", "skipped")},
            "test_seconds": sum(times),
            "suite_seconds": sum(suite_times),
            "slowest_tests": sorted(slow, key=lambda row: (-row["seconds"], row["test"]))[:20],
        }
        record["metrics_status"] = "AVAILABLE"
    except (OSError, ElementTree.ParseError, ValueError) as exc:
        record["metrics_status"] = "METRICS_UNAVAILABLE"
        record["metrics_error"] = str(exc)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execution-kind", choices=("full", "selected"), required=True)
    parser.add_argument("--planner", type=Path)
    args = parser.parse_args()
    identity = json.loads(args.identity.read_text())
    environment = identity["environment"]
    identity["environment_key"] = (
        hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
        if all(environment.values()) else None
    )
    record = collect(args.junit, identity, args.execution_kind,
                     json.loads(args.planner.read_text()) if args.planner else None)
    args.output.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as stream:
            stream.write("### Test timing (observational)\n\n")
            if record["metrics_status"] == "AVAILABLE":
                data = record["pytest"]
                stream.write(f"{data['test_count']} cases; {data['test_seconds']:.3f}s summed test time\n\n")
                for row in data["slowest_tests"]:
                    # Test output is untrusted; keep names inside escaped inline text.
                    name = row["test"].replace("`", "'").replace("\n", " ").replace("\r", " ")
                    stream.write(f"- `{name}`: {row['seconds']:.3f}s\n")
            else:
                stream.write("METRICS_UNAVAILABLE; Quality result remains authoritative.\n")


if __name__ == "__main__":
    main()
