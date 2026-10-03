#!/usr/bin/env python3
"""Report retained fixed-base package size without storing or setting policy."""

import argparse
import fnmatch
import json
import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def commit(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def dimension(actual: int, forecast: int, cap: int) -> dict[str, object]:
    over = actual - forecast
    return {
        "actual": actual,
        "original_upper_forecast": forecast,
        "over_forecast": over,
        "forecast_miss_trigger": actual * 2 >= forecast * 3 and over >= 100,
        "hard_cap": cap,
        "cap_headroom": cap - actual,
        "hard_cap_breached": actual > cap,
    }


def changes(repo: Path, base: str, head: str):
    parts = git(repo, "diff", "--numstat", "-z", "-M", base, head, "--").split(b"\0")
    index = 0
    while index < len(parts):
        record = parts[index]
        index += 1
        if not record:
            continue
        fields = record.split(b"\t")
        if len(fields) != 3:
            raise ValueError("unexpected git --numstat -z record")
        added, deleted, path = fields
        if path:
            decoded = path.decode(errors="surrogateescape")
            yield added, deleted, decoded, decoded
            continue
        if index + 1 >= len(parts):
            raise ValueError("truncated git rename record")
        old = parts[index].decode(errors="surrogateescape")
        new = parts[index + 1].decode(errors="surrogateescape")
        index += 2
        yield added, deleted, old, new


def build_report(args: argparse.Namespace) -> dict[str, object]:
    forecasts_caps = (
        args.production_forecast,
        args.support_forecast,
        args.production_cap,
        args.support_cap,
        args.total_cap,
    )
    if min(forecasts_caps) < 0:
        raise ValueError("forecasts and caps must be non-negative")
    if args.production_cap < args.production_forecast:
        raise ValueError("production cap cannot be below original forecast")
    if args.support_cap < args.support_forecast:
        raise ValueError("support cap cannot be below original forecast")
    if args.total_cap < args.production_forecast + args.support_forecast:
        raise ValueError("total cap cannot be below original total forecast")

    base = commit(args.repo, args.base)
    head = commit(args.repo, args.head)
    counts = {"production": 0, "support": 0}
    files = []
    unknown = []

    for added, deleted, old, new in changes(args.repo, base, head):
        old_in = matches(old, args.package)
        new_in = matches(new, args.package)
        if not old_in and not new_in:
            continue
        if old_in != new_in:
            unknown.append(f"rename crosses package scope: {old!r} -> {new!r}")
            continue
        old_support = matches(old, args.support)
        new_support = matches(new, args.support)
        if old_support != new_support:
            unknown.append(f"rename changes production/support class: {old!r} -> {new!r}")
            continue
        if added == b"-" or deleted == b"-":
            unknown.append(f"uncountable/binary package change: {old!r} -> {new!r}")
            continue
        gross = int(added) + int(deleted)
        category = "support" if old_support else "production"
        counts[category] += gross
        files.append({"old": old, "new": new, "category": category, "gross": gross})

    production = dimension(
        counts["production"], args.production_forecast, args.production_cap
    )
    support = dimension(counts["support"], args.support_forecast, args.support_cap)
    total_actual = counts["production"] + counts["support"]
    total = dimension(
        total_actual,
        args.production_forecast + args.support_forecast,
        args.total_cap,
    )
    total["forecast_miss_trigger"] = bool(
        production["forecast_miss_trigger"] or support["forecast_miss_trigger"]
    )
    return {
        "status": "UNKNOWN" if unknown else "OK",
        "base": base,
        "head": head,
        "package_patterns": args.package,
        "support_patterns": args.support,
        "production": production,
        "support": support,
        "total": total,
        "files": files,
        "unknown": unknown,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--repo", type=Path, default=Path("."))
    result.add_argument("--base", required=True)
    result.add_argument("--head", default="HEAD")
    result.add_argument("--package", action="append", required=True)
    result.add_argument("--support", action="append", default=[])
    result.add_argument("--production-forecast", type=int, required=True)
    result.add_argument("--support-forecast", type=int, required=True)
    result.add_argument("--production-cap", type=int, required=True)
    result.add_argument("--support-cap", type=int, required=True)
    result.add_argument("--total-cap", type=int, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        report = build_report(args)
    except ValueError as exc:
        print(json.dumps({"status": "UNKNOWN", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2 if report["status"] == "UNKNOWN" else 0


if __name__ == "__main__":
    raise SystemExit(main())
