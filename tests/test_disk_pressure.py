from datetime import UTC, datetime
from pathlib import Path

from switchstand.disk_pressure import check
from switchstand.wakeful import WakefulStore

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
GIB = 1024**3


def run(tmp_path: Path, free_gib: list[int]):
    def usage(_path: Path):
        free = free_gib[-1] * GIB
        return 400 * GIB, 400 * GIB - free, free

    return check(tmp_path, clock=lambda: NOW, usage_probe=usage)


def test_warning_is_deduplicated_and_store_is_private(tmp_path: Path) -> None:
    free = [60]
    store = WakefulStore(tmp_path / "disk-pressure/wakeful.sqlite3")

    first = run(tmp_path, free)
    second = run(tmp_path, free)

    assert first is not None and first.kind == "disk.warning"
    assert second is None
    assert len(store.pending()) == 1
    event = store.event(first.event_id)
    assert event is not None and event.kind == "disk.warning"
    assert "60.0 GiB free" in event.summary
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_recovery_requires_two_clear_cycles(tmp_path: Path) -> None:
    free = [20]
    failure = run(tmp_path, free)
    assert failure is not None and failure.kind == "disk.critical"

    free[-1] = 100
    first_clear = run(tmp_path, free)
    second_clear = run(tmp_path, free)

    assert first_clear is None
    assert second_clear is not None and second_clear.kind == "disk.recovered"
    store = WakefulStore(tmp_path / "disk-pressure/wakeful.sqlite3")
    assert {event.kind for event in store.pending()} == {
        "disk.critical",
        "disk.recovered",
    }


def test_overlapping_cycle_coalesces(tmp_path: Path) -> None:
    store = WakefulStore(tmp_path / "disk-pressure/wakeful.sqlite3")
    lease = store.acquire_cycle(monitor="root-filesystem-pressure", now=NOW, lease_seconds=300)
    assert lease is not None
    assert run(tmp_path, [60]) is None
    store.abandon_cycle(lease)
