"""Asynchronous disk-pressure observation and durable Wakeful transitions."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .wakeful import EventSeverity, WakeEvent, WakefulStore

GIB = 1024**3


Usage = tuple[int, int, int]
Clock = Callable[[], datetime]
UsageProbe = Callable[[Path], Usage]


def _clock() -> datetime:
    return datetime.now(UTC)


def _usage(path: Path) -> Usage:
    usage = shutil.disk_usage(path)
    return usage.total, usage.used, usage.free


def default_state_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "switchstand"


def check(
    state_root: Path,
    filesystem: Path = Path("/"),
    warning_free_percent: float = 20.0,
    critical_free_percent: float = 10.0,
    clock: Clock = _clock,
    usage_probe: UsageProbe = _usage,
) -> WakeEvent | None:
    """Run one off-launch pressure audit and enqueue only condition transitions."""
    if not 0 < critical_free_percent < warning_free_percent < 100:
        raise ValueError("disk thresholds must satisfy 0 < critical < warning < 100")
    store = WakefulStore(state_root / "disk-pressure/wakeful.sqlite3")
    started = clock()
    lease = store.acquire_cycle(monitor="root-filesystem-pressure", now=started, lease_seconds=300)
    if lease is None:
        return None
    try:
        total, used, free = usage_probe(filesystem)
        if total <= 0 or min(used, free) < 0:
            raise ValueError("invalid filesystem usage")
        free_percent = free * 100 / total
        condition = (
            "critical"
            if free_percent <= critical_free_percent
            else "warning"
            if free_percent <= warning_free_percent
            else "healthy"
        )
        event = WakeEvent.create(
            source="switchstand.disk-pressure",
            subject=str(filesystem),
            kind=("disk.recovered" if condition == "healthy" else f"disk.{condition}"),
            severity=(
                EventSeverity.INFO
                if condition == "healthy"
                else EventSeverity.WARNING
                if condition == "warning"
                else EventSeverity.CRITICAL
            ),
            summary=f"Disk {condition}: {free / GIB:.1f} GiB free ({free_percent:.1f}%)",
            observed_at=started,
        )
        emitted = store.complete_cycle(
            lease=lease,
            cursor=started.isoformat(),
            condition=condition,
            healthy=condition == "healthy",
            event=event,
            now=clock(),
        )
        return event if emitted else None
    except BaseException:
        store.abandon_cycle(lease)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "status"), nargs="?", default="check")
    parser.add_argument("--state-root", type=Path, default=default_state_root())
    parser.add_argument("--filesystem", type=Path, default=Path("/"))
    parser.add_argument("--warning-free-percent", type=float, default=20.0)
    parser.add_argument("--critical-free-percent", type=float, default=10.0)
    parser.add_argument("--event-id")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    store = WakefulStore(arguments.state_root / "disk-pressure/wakeful.sqlite3")
    if arguments.command == "check":
        event = check(
            arguments.state_root,
            filesystem=arguments.filesystem,
            warning_free_percent=arguments.warning_free_percent,
            critical_free_percent=arguments.critical_free_percent,
        )
        print(json.dumps(None if event is None else asdict(event), sort_keys=True))
        return
    if not arguments.event_id:
        raise SystemExit("--event-id is required for status")
    event = store.event(arguments.event_id)
    if event is None:
        raise SystemExit("disk-pressure event not found")
    print(json.dumps(asdict(event), sort_keys=True))


if __name__ == "__main__":
    main()
