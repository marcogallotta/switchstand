import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from switchstand.agent_broker import Broker, Budget, Pressure
from switchstand.agent_canary import Canary
from switchstand.agent_canary_probe import probe

GREEN = Pressure(16_000, 8_000, 4_000, 0, 0)
RED = Pressure(16_000, 8_000, 4_000, 3_000, 0)
ROOT = Budget(2100, 3072, 384, 300, 288, 3, 0)


class FakeRunner:
    def __init__(
        self, broker: Broker, state: str = "completed", bad_file: str | None = None
    ) -> None:
        self.broker = broker
        self.state = state
        self.bad_file = bad_file
        self.calls: list[str] = []

    def run(
        self, lease_id: str, command: list[str], working_directory: Path, timeout: int
    ) -> dict[str, Any]:
        self.calls.append(lease_id)
        receipt: dict[str, Any] = {"lease_id": lease_id, "state": self.state}
        self.broker.claim_execution(lease_id, {"state": "starting"})
        execution = self.broker.execution_dir(lease_id)
        relative = command[-1]
        payload = (working_directory / relative).read_bytes()
        proof = {
            "path": relative,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "docker_socket": "blocked",
            "cgroup": {
                "memory.high": str(700 * 1024 * 1024),
                "memory.max": str(1024 * 1024 * 1024),
                "memory.swap.max": str(128 * 1024 * 1024),
                "cpu.max": "100000 100000",
                "pids.max": "96",
            },
        }
        if self.bad_file:
            if self.bad_file.startswith("missing:"):
                proof.pop(self.bad_file.removeprefix("missing:"))
            else:
                proof[self.bad_file] = "wrong"
        (execution / "stdout.log").write_text(json.dumps(proof) + "\n")
        if self.state == "completed":
            lease = self.broker.lease(lease_id)
            self.broker.reconcile_execution(
                lease_id,
                reservation_id=lease["reservation_id"],
                attempt_id=f"attempt-{lease_id}",
                unit=f"unknown-{lease_id}.service",
                observed_boot_id=self.broker.boot_id,
                execution_started=True,
                unit_terminal=True,
                cgroup_empty=True,
            )
        self.broker.record_execution(lease_id, receipt)
        return receipt


def initialized(tmp_path: Path) -> Broker:
    broker = Broker(tmp_path / "broker")
    broker.initialize(ROOT)
    return broker


def test_canary_proves_two_workers_denial_cancel_and_durable_outputs(tmp_path: Path) -> None:
    broker = initialized(tmp_path)
    runner = FakeRunner(broker)
    report = Canary(broker, runner).run("proof", Path.cwd(), GREEN)

    assert report["state"] == "passed"
    assert runner.calls == ["proof-worker-0", "proof-worker-1"]
    assert report["third_worker"]["reason"] == "parent_budget"
    assert report["recursive_cancel"]["expected"] == report["recursive_cancel"]["observed"]
    assert report["lease_states"]["proof-parent"] == "completed"
    assert all(report["lease_states"][f"proof-cancel-{index}"] == "cancelled" for index in range(2))
    assert all((broker.execution_dir(worker) / "stdout.log").is_file() for worker in runner.calls)
    assert all(item["valid"] for item in report["worker_evidence"])
    assert json.loads(Path(report["report_path"]).read_text())["state"] == "passed"


def test_pressure_refusal_never_reserves_or_executes(tmp_path: Path) -> None:
    broker = initialized(tmp_path)
    runner = FakeRunner(broker)
    report = Canary(broker, runner).run("pressure", Path.cwd(), RED)

    assert report["state"] == "refused_during_admission"
    assert report["reason"] == "host_pressure"
    assert runner.calls == []
    assert broker.status()["leases"] == {}
    assert Path(report["report_path"]).is_file()


def test_live_pressure_is_resampled_and_red_stops_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = initialized(tmp_path)
    runner = FakeRunner(broker)
    samples = iter([GREEN, GREEN, RED])
    monkeypatch.setattr(Pressure, "current", classmethod(lambda cls: next(samples)))

    report = Canary(broker, runner).run("resample", Path.cwd())

    assert report["state"] == "refused_during_admission"
    assert [item["result"]["state"] for item in report["admissions"]] == [
        "reserved",
        "reserved",
        "refused",
    ]
    assert runner.calls == []
    assert all(item["state"] == "cancelled" for item in broker.status()["leases"].values())


def test_pressure_on_third_denial_cleans_all_prelaunch_leases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = initialized(tmp_path)
    runner = FakeRunner(broker)
    samples = iter([*[GREEN] * 6, RED])
    monkeypatch.setattr(Pressure, "current", classmethod(lambda cls: next(samples)))

    report = Canary(broker, runner).run("thirdred", Path.cwd())

    assert report["state"] == "refused_during_admission"
    assert report["reason"] == "host_pressure"
    assert len(report["admissions"]) == 7
    assert runner.calls == []
    assert all(item["state"] == "cancelled" for item in broker.status()["leases"].values())
    assert json.loads(Path(report["report_path"]).read_text())["state"] == report["state"]


def test_unknown_worker_keeps_capacity_and_reports_incomplete(tmp_path: Path) -> None:
    broker = initialized(tmp_path)
    report = Canary(broker, FakeRunner(broker, "unknown")).run("unknown", Path.cwd(), GREEN)

    assert report["state"] == "incomplete"
    assert report["lease_states"]["unknown-parent"] == "reserved"
    assert report["lease_states"]["unknown-worker-0"] == "execution_active"
    assert report["lease_states"]["unknown-worker-1"] == "execution_active"


@pytest.mark.parametrize("field", ["path", "sha256", "missing:path", "missing:sha256"])
def test_controller_rejects_wrong_file_identity(tmp_path: Path, field: str) -> None:
    broker = initialized(tmp_path)
    report = Canary(broker, FakeRunner(broker, bad_file=field)).run("badfile", Path.cwd(), GREEN)

    assert report["state"] == "incomplete"
    assert not all(item["valid"] for item in report["worker_evidence"])


def test_probe_reads_only_and_requires_docker_socket_to_be_hidden(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "evidence.txt").write_text("evidence")
    original_stat = os.stat

    def hidden(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if str(path) in {"/run/docker.sock", "/var/run/docker.sock"}:
            raise PermissionError
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", hidden)
    cgroup = tmp_path / "cgroup/test"
    cgroup.mkdir(parents=True)
    for name, value in {
        "memory.high": "734003200",
        "memory.max": "1073741824",
        "memory.swap.max": "134217728",
        "cpu.max": "100000 100000",
        "pids.max": "96",
    }.items():
        (cgroup / name).write_text(value)
    result = probe("evidence.txt", tmp_path / "cgroup", "test")
    assert result["bytes"] == 8
    assert result["docker_socket"] == "blocked"


def test_probe_fails_if_docker_socket_is_visible(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "evidence.txt").write_text("evidence")
    monkeypatch.setattr(os, "stat", lambda *args, **kwargs: object())
    with pytest.raises(RuntimeError, match="Docker socket visible"):
        probe("evidence.txt", tmp_path, "unused")
