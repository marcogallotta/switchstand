"""Non-authoritative, reusable JUnit timing records for GitHub artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


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
