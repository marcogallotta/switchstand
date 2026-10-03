"""Prepare and run the explicit two-worker resource-governance canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from switchstand.agent_broker import CLASSES, Broker, Pressure
from switchstand.agent_executor import Executor

CHILDREN = {**asdict(CLASSES["light"].add(CLASSES["light"])), "heavy": 0}
for _field in ("workers", "heavy"):
    CHILDREN[_field] = 2 if _field == "workers" else 0


class Runner(Protocol):
    def run(
        self, lease_id: str, command: list[str], working_directory: Path, timeout: int
    ) -> dict[str, Any]: ...


class AdmissionStopped(RuntimeError):
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result


class Canary:
    def __init__(self, broker: Broker, runner: Runner | None = None) -> None:
        self.broker = broker
        self.runner = runner or Executor(broker)

    def run(self, run_id: str, workdir: Path, pressure: Pressure | None = None) -> dict[str, Any]:
        if len(run_id) > 40:
            raise ValueError("run identifier is too long")
        workdir = workdir.resolve(strict=True)
        files = ("pyproject.toml", "AGENTS.md")
        expected = [self._expected_file(workdir, relative) for relative in files]
        admissions: list[dict[str, Any]] = []
        report: dict[str, Any] = {"run_id": run_id, "admissions": admissions}
        report = self._record(report | {"state": "starting"})
        active_roots: list[str] = []

        try:
            rehearsal = f"{run_id}-cancel"
            self._reserve("root", f"{run_id}-cr", rehearsal, CHILDREN, pressure, admissions)
            active_roots.append(rehearsal)
            cancelled_children: list[str] = []
            for index in range(2):
                worker = f"{run_id}-cancel-{index}"
                self._reserve(rehearsal, f"{run_id}-c{index}", worker, {}, pressure, admissions)
                cancelled_children.append(worker)
            cancelled = self.broker.cancel(rehearsal)
            active_roots.remove(rehearsal)
            cancelled_expected = sorted([rehearsal, *cancelled_children])

            parent = f"{run_id}-parent"
            self._reserve("root", f"{run_id}-pr", parent, CHILDREN, pressure, admissions)
            active_roots.append(parent)
            workers: list[str] = []
            for index in range(2):
                worker = f"{run_id}-worker-{index}"
                self._reserve(parent, f"{run_id}-w{index}", worker, {}, pressure, admissions)
                workers.append(worker)
            third = self._request(
                parent, f"{run_id}-deny", f"{run_id}-worker-2", {}, pressure, admissions
            )
            if third.get("state") != "refused" or third.get("reason") != "parent_budget":
                raise AdmissionStopped(third)
        except AdmissionStopped as stopped:
            for active in active_roots:
                self.broker.cancel(active)
            state = (
                "refused_during_admission"
                if stopped.result.get("state") == "refused"
                else "admission_error"
            )
            return self._record(
                report
                | {
                    "state": state,
                    "reason": stopped.result.get("reason"),
                    "refusal": stopped.result,
                    "admissions": admissions,
                }
            )

        probe = Path(__file__).with_name("agent_canary_probe.py")
        receipts: list[dict[str, Any]] = [
            self.runner.run(
                worker,
                ["/usr/bin/python3", str(probe), relative],
                workdir,
                120,
            )
            for worker, relative in zip(workers, files, strict=True)
        ]
        evidence = [
            self._worker_evidence(worker, expected_file)
            for worker, expected_file in zip(workers, expected, strict=True)
        ]
        successful = (
            sorted(cancelled) == cancelled_expected
            and all(receipt.get("state") == "completed" for receipt in receipts)
            and all(item["valid"] for item in evidence)
        )
        if successful:
            self.broker.complete(parent)
        states = self.broker.status()["leases"]
        return self._record(
            report
            | {
                "state": "passed" if successful else "incomplete",
                "recursive_cancel": {
                    "expected": cancelled_expected,
                    "observed": sorted(cancelled),
                },
                "third_worker": third,
                "workers": receipts,
                "worker_evidence": evidence,
                "lease_states": {key: value["state"] for key, value in states.items()},
            }
        )

    def _reserve(
        self,
        parent: str,
        request_id: str,
        worker: str,
        children: dict[str, int],
        pressure: Pressure | None,
        admissions: list[dict[str, Any]],
    ) -> None:
        result = self._request(parent, request_id, worker, children, pressure, admissions)
        if result.get("state") != "reserved":
            raise AdmissionStopped(result)

    def _request(
        self,
        parent: str,
        request_id: str,
        worker: str,
        children: dict[str, int],
        pressure: Pressure | None,
        admissions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        zero = {key: 0 for key in asdict(CLASSES["light"])}
        body = {
            "request_id": request_id,
            "parent": parent,
            "worker": worker,
            "worker_class": "light",
            "children": zero | children,
        }
        path = self.broker.root / "inboxes" / parent / f"{request_id}.json"
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as handle:
            json.dump(body, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        result = self.broker.ingest(parent, request_id, pressure)
        observed = asdict(pressure) if pressure is not None else {"source": "broker_live_sample"}
        admissions.append({"request_id": request_id, "pressure": observed, "result": result})
        return result

    @staticmethod
    def _expected_file(workdir: Path, relative: str) -> dict[str, Any]:
        payload = (workdir / relative).read_bytes()
        return {
            "path": relative,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def _worker_evidence(self, worker: str, expected_file: dict[str, Any]) -> dict[str, Any]:
        path = self.broker.execution_dir(worker) / "stdout.log"
        try:
            value = json.loads(path.read_text())
            cgroup = value["cgroup"]
            cpu_quota, cpu_period = (int(item) for item in cgroup["cpu.max"].split())
            expected = CLASSES["light"]
            valid = (
                value["docker_socket"] == "blocked"
                and all(value.get(key) == expected_file[key] for key in ("path", "bytes", "sha256"))
                and int(cgroup["memory.high"]) == expected.memory_high_mib * 1024 * 1024
                and int(cgroup["memory.max"]) == expected.memory_max_mib * 1024 * 1024
                and int(cgroup["memory.swap.max"]) == expected.swap_max_mib * 1024 * 1024
                and int(cgroup["pids.max"]) == expected.tasks
                and 100 * cpu_quota // cpu_period == expected.cpu_percent
            )
        except KeyError, OSError, TypeError, ValueError, json.JSONDecodeError:
            value, valid = {}, False
        return {"lease_id": worker, "output": str(path), "proof": value, "valid": valid}

    def _record(self, report: dict[str, Any]) -> dict[str, Any]:
        report["report_path"] = str(
            self.broker.root / "canaries" / report["run_id"] / "report.json"
        )
        self.broker.record_canary(report["run_id"], report)
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("workdir", type=Path)
    args = parser.parse_args()
    print(json.dumps(Canary(Broker()).run(args.run_id, args.workdir), sort_keys=True))
