import json
from pathlib import Path

import pytest

from switchstand.agent_broker import Broker, Budget, Pressure

ROOT = Budget(4096, 6144, 512, 400, 512, 4, 1)
GREEN = Pressure(16_000, 8_000, 4_000, 0, 0)


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


def test_pressure_refuses_without_reservation(tmp_path: Path) -> None:
    broker = Broker(tmp_path)
    broker.initialize(ROOT)
    request(tmp_path, "root", "r1", "worker")
    pressure = Pressure(16_000, 8_000, 4_000, 2_000, 0)
    assert broker.ingest("root", "r1", pressure)["reason"] == "host_pressure"
    assert broker.status()["leases"] == {}


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
