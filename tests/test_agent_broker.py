import json
import threading
from pathlib import Path

import pytest

from switchstand.agent_broker import Broker, Budget, Pressure

ROOT = Budget(4096, 6144, 512, 400, 512, 4, 1)
GREEN = Pressure(16_000, 12_000, 4_000, 0, 0, swap_activity_pages=0)


def request(
    root: Path,
    parent: str,
    request_id: str,
    worker: str,
    worker_class: str = "light",
    **children: int,
) -> None:
    values = {
        "memory_high_mib": 0,
        "memory_max_mib": 0,
        "swap_max_mib": 0,
        "cpu_percent": 0,
        "tasks": 0,
        "workers": 0,
        "heavy": 0,
    }
    values.update(children)
    path = root / "inboxes" / parent / f"{request_id}.json"
    path.write_text(
        json.dumps(
            {
                "request_id": request_id,
                "parent": parent,
                "worker": worker,
                "worker_class": worker_class,
                "children": values,
            }
        )
    )
    path.chmod(0o600)


def parent_request(root: Path) -> None:
    request(
        root,
        "root",
        "r1",
        "parent",
        memory_max_mib=1200,
        memory_high_mib=1000,
        swap_max_mib=128,
        cpu_percent=100,
        tasks=100,
        workers=1,
    )


def test_hierarchy_subdivides_and_deduplicates(tmp_path: Path) -> None:
    broker = Broker(tmp_path)
    broker.initialize(ROOT)
    parent_request(tmp_path)
    assert broker.ingest("root", "r1", GREEN)["state"] == "reserved"
    assert broker.ingest("root", "r1", GREEN)["lease_id"] == "parent"
    assert (tmp_path / "results/r1.json").stat().st_mode & 0o777 == 0o600

    request(tmp_path, "parent", "r2", "child")
    assert broker.ingest("parent", "r2", GREEN)["state"] == "reserved"
    request(tmp_path, "parent", "r3", "overflow")
    assert broker.ingest("parent", "r3", GREEN) == {
        "request_id": "r3",
        "parent": "parent",
        "state": "refused",
        "reason": "parent_budget",
    }


def test_parent_cannot_complete_and_cancel_is_recursive(tmp_path: Path) -> None:
    broker = Broker(tmp_path)
    broker.initialize(ROOT)
    parent_request(tmp_path)
    broker.ingest("root", "r1", GREEN)
    request(tmp_path, "parent", "r2", "child")
    broker.ingest("parent", "r2", GREEN)
    with pytest.raises(RuntimeError, match="active children"):
        broker.complete("parent")
    assert set(broker.cancel("parent")) == {"parent", "child"}
    assert {item["state"] for item in broker.status()["leases"].values()} == {"cancelled"}


def test_historical_swap_without_recent_movement_admits_light(tmp_path: Path) -> None:
    broker = Broker(tmp_path)
    broker.initialize(ROOT)
    request(tmp_path, "root", "r1", "worker")
    pressure = Pressure(16_000, 12_000, 4_000, 3_900, 0, swap_activity_pages=0)
    assert broker.ingest("root", "r1", pressure)["state"] == "reserved"


@pytest.mark.parametrize(
    ("pressure", "reason"),
    [
        (
            Pressure(16_000, 12_000, 4_000, 3_900, 0, swap_activity_pages=1),
            "host_pressure",
        ),
        (
            Pressure(16_000, 12_000, 4_000, 0, 5, swap_activity_pages=0),
            "host_pressure",
        ),
        (
            Pressure(
                16_000,
                12_000,
                4_000,
                0,
                0,
                swap_activity_pages=None,
                pswpin=10,
                pswpout=10,
            ),
            "pressure_sample_missing",
        ),
        (
            Pressure(
                16_000,
                12_000,
                4_000,
                0,
                0,
                swap_activity_pages=0,
                sampled_monotonic=1,
            ),
            "pressure_sample_stale",
        ),
        (
            Pressure(16_000, 12_000, 4_000, 0, 0, swap_activity_pages=-1),
            "pressure_sample_invalid",
        ),
    ],
)
def test_pressure_refuses_without_reservation(
    tmp_path: Path, pressure: Pressure, reason: str
) -> None:
    broker = Broker(tmp_path, clock=lambda: 10.0)
    broker.initialize(ROOT)
    request(tmp_path, "root", "r1", "worker")
    result = broker.ingest("root", "r1", pressure)
    assert result["reason"] == reason
    assert broker.status()["leases"] == {}


def test_live_vmstat_samples_use_monotonic_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = Broker(tmp_path, clock=lambda: 12.0)
    broker.initialize(ROOT)
    samples = iter(
        [
            Pressure(16_000, 12_000, 4_000, 3_900, 0, sampled_monotonic=10, pswpin=5, pswpout=7),
            Pressure(16_000, 12_000, 4_000, 3_900, 0, sampled_monotonic=11, pswpin=6, pswpout=7),
        ]
    )

    def next_sample() -> Pressure:
        return next(samples)

    monkeypatch.setattr(Pressure, "current", staticmethod(next_sample))
    request(tmp_path, "root", "r1", "first")
    request(tmp_path, "root", "r2", "second")

    assert broker.ingest("root", "r1")["reason"] == "pressure_sample_missing"
    assert broker.ingest("root", "r2")["reason"] == "host_pressure"
    assert broker.status()["leases"] == {}


def test_available_memory_includes_atomic_top_level_reservations(tmp_path: Path) -> None:
    broker = Broker(tmp_path)
    broker.initialize(Budget(4096, 4096, 512, 400, 512, 4, 1))
    request(tmp_path, "root", "r1", "one")
    request(tmp_path, "root", "r2", "two")
    pressure = Pressure(8_000, 4_000, 4_000, 0, 0, swap_activity_pages=0)
    barrier = threading.Barrier(2)
    results: list[dict[str, object]] = []

    def admit(request_id: str) -> None:
        barrier.wait()
        results.append(broker.ingest("root", request_id, pressure))

    first = threading.Thread(target=admit, args=("r1",))
    second = threading.Thread(target=admit, args=("r2",))
    first.start()
    second.start()
    first.join()
    second.join()

    assert sorted(str(result["state"]) for result in results) == ["refused", "reserved"]
    assert next(result for result in results if result["state"] == "refused")["reason"] == (
        "memory_available"
    )


def test_nested_admission_does_not_double_count_parent_allowance(tmp_path: Path) -> None:
    broker = Broker(tmp_path)
    broker.initialize(ROOT)
    parent_request(tmp_path)
    pressure = Pressure(8_000, 4_300, 4_000, 0, 0, swap_activity_pages=0)
    assert broker.ingest("root", "r1", pressure)["state"] == "reserved"
    request(tmp_path, "parent", "r2", "child")
    assert broker.ingest("parent", "r2", pressure)["state"] == "reserved"


def test_reconcile_releases_only_expired_proven_not_started_attempt(tmp_path: Path) -> None:
    now = 10.0
    broker = Broker(tmp_path, clock=lambda: now, boot_id="boot-a")
    broker.initialize(ROOT)
    request(tmp_path, "root", "r1", "worker")
    result = broker.ingest("root", "r1", GREEN)
    broker.attach_execution(
        "worker",
        reservation_id=result["reservation_id"],
        attempt_id="attempt-1",
        unit="worker.service",
        receipt={"state": "starting"},
        claim_ttl_seconds=5,
    )

    reservation_id = str(result["reservation_id"])

    def reconcile(observed: float) -> dict[str, str]:
        return broker.reconcile_execution(
            "worker",
            reservation_id=reservation_id,
            attempt_id="attempt-1",
            unit="worker.service",
            observed_boot_id="boot-a",
            execution_started=False,
            unit_terminal=None,
            cgroup_empty=None,
            observed_monotonic=observed,
        )

    assert reconcile(14) == {
        "state": "unknown",
        "reason": "runtime_ambiguous",
    }
    assert reconcile(15) == {
        "state": "released",
        "reason": "abandoned",
    }
    assert broker.status()["leases"]["worker"]["state"] == "abandoned"


def test_unattached_reservation_releases_only_after_expiry_and_absence_proof(
    tmp_path: Path,
) -> None:
    broker = Broker(tmp_path, clock=lambda: 10.0, boot_id="boot-a")
    broker.initialize(ROOT)
    request(tmp_path, "root", "r1", "worker")
    result = broker.ingest("root", "r1", GREEN)

    def reconcile(observed: float, absent: bool | None) -> dict[str, str]:
        return broker.reconcile_reservation(
            "worker",
            reservation_id=result["reservation_id"],
            observed_boot_id="boot-a",
            launch_absent=absent,
            observed_monotonic=observed,
            reservation_ttl_seconds=5,
        )

    assert reconcile(14, True)["state"] == "unknown"
    assert reconcile(15, None)["state"] == "unknown"
    assert reconcile(15, True) == {"state": "released", "reason": "abandoned"}


def test_proof_free_finish_path_is_rejected(tmp_path: Path) -> None:
    broker = Broker(tmp_path)
    broker.initialize(ROOT)
    request(tmp_path, "root", "r1", "worker")
    result = broker.ingest("root", "r1", GREEN)
    broker.attach_execution(
        "worker",
        reservation_id=result["reservation_id"],
        attempt_id="attempt-1",
        unit="worker.service",
        receipt={"state": "starting"},
    )

    with pytest.raises(RuntimeError, match="runtime reconciliation"):
        broker.finish_execution("worker", "completed")
    assert broker.lease("worker")["state"] == "execution_active"


@pytest.mark.parametrize(
    ("boot_id", "started", "terminal", "empty", "reason"),
    [
        ("other-boot", True, True, True, "boot_identity"),
        ("boot-a", None, None, None, "runtime_ambiguous"),
        ("boot-a", True, True, False, "runtime_ambiguous"),
        ("boot-a", True, False, True, "runtime_ambiguous"),
    ],
)
def test_reconcile_preserves_ambiguous_or_nonempty_execution(
    tmp_path: Path,
    boot_id: str,
    started: bool | None,
    terminal: bool | None,
    empty: bool | None,
    reason: str,
) -> None:
    broker = Broker(tmp_path, clock=lambda: 20.0, boot_id="boot-a")
    broker.initialize(ROOT)
    request(tmp_path, "root", "r1", "worker")
    result = broker.ingest("root", "r1", GREEN)
    broker.attach_execution(
        "worker",
        reservation_id=result["reservation_id"],
        attempt_id="attempt-1",
        unit="worker.service",
        receipt={"state": "starting"},
    )

    assert broker.reconcile_execution(
        "worker",
        reservation_id=result["reservation_id"],
        attempt_id="attempt-1",
        unit="worker.service",
        observed_boot_id=boot_id,
        execution_started=started,
        unit_terminal=terminal,
        cgroup_empty=empty,
    ) == {"state": "unknown", "reason": reason}
    assert broker.status()["leases"]["worker"]["state"] == "execution_active"


def test_reconcile_releases_terminal_unit_only_with_empty_cgroup(tmp_path: Path) -> None:
    broker = Broker(tmp_path, clock=lambda: 20.0, boot_id="boot-a")
    broker.initialize(ROOT)
    request(tmp_path, "root", "r1", "worker")
    result = broker.ingest("root", "r1", GREEN)
    broker.attach_execution(
        "worker",
        reservation_id=result["reservation_id"],
        attempt_id="attempt-1",
        unit="worker.service",
        receipt={"state": "starting"},
    )

    assert broker.reconcile_execution(
        "worker",
        reservation_id=result["reservation_id"],
        attempt_id="attempt-1",
        unit="worker.service",
        observed_boot_id="boot-a",
        execution_started=True,
        unit_terminal=True,
        cgroup_empty=True,
    ) == {"state": "released", "reason": "completed"}


def test_strict_schema_and_hostile_spool_are_rejected(tmp_path: Path) -> None:
    broker = Broker(tmp_path)
    broker.initialize(ROOT)
    request(tmp_path, "root", "bad", "worker")
    body = json.loads((tmp_path / "inboxes/root/bad.json").read_text())
    body["surprise"] = True
    (tmp_path / "inboxes/root/bad.json").write_text(json.dumps(body))
    with pytest.raises(ValueError):
        broker.ingest("root", "bad", GREEN)

    request(tmp_path, "root", "reserved", "root")
    with pytest.raises(ValueError, match="identifier"):
        broker.ingest("root", "reserved", GREEN)
    assert broker.status()["leases"] == {}

    real = tmp_path / "inboxes/real"
    (tmp_path / "inboxes/root").rename(real)
    (tmp_path / "inboxes/root").symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        broker.ingest("root", "reserved", GREEN)
