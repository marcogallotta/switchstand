"""Default-off systemd executor for an already-reserved leaf agent lease."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Literal

from switchstand.agent_broker import Broker
from switchstand.managed_launch import PreparedLaunch, PreparedLaunchStore

HOST = "marco@.host"
RUN_TIMEOUT_SECONDS = 3600
STOP_TIMEOUT_SECONDS = 15
SANDBOX = (
    "NoNewPrivileges=yes",
    "PrivateDevices=yes",
    "PrivateTmp=yes",
    "ProtectControlGroups=yes",
    "ProtectHome=tmpfs",
    "ProtectKernelModules=yes",
    "ProtectKernelTunables=yes",
    "ProtectSystem=strict",
    "RestrictNamespaces=yes",
    "RestrictSUIDSGID=yes",
    "InaccessiblePaths=/run/docker.sock /var/run/docker.sock /run/user/%U/bus",
)
MANAGED_SANDBOX = tuple(
    item for item in SANDBOX if not item.startswith(("ProtectHome=", "InaccessiblePaths="))
) + ("ProtectHome=read-only",)


class Executor:
    def __init__(self, broker: Broker | None = None) -> None:
        self.broker = broker or Broker()

    def run(
        self,
        lease_id: str,
        command: Sequence[str],
        working_directory: Path,
        timeout: int = RUN_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        if not command or timeout <= 0:
            raise ValueError("command and a positive timeout are required")
        lease = self.broker.lease(lease_id)
        if any(lease["children"].values()):
            raise ValueError("Layer 2 executes leaf leases only")
        workdir = working_directory.resolve(strict=True)
        if not workdir.is_dir():
            raise ValueError("working directory is not a directory")
        if any(character.isspace() or character in ":\\%" for character in str(workdir)):
            raise ValueError("working directory cannot be safely encoded for systemd")
        unit = self._unit(lease_id)
        execution = self.broker.execution_dir(lease_id)
        receipt: dict[str, Any] = {"lease_id": lease_id, "unit": unit, "state": "starting"}
        self.broker.claim_execution(lease_id, receipt)
        with ExitStack() as opened:
            try:
                stdout = self._output(execution / "stdout.log")
                opened.callback(self._close_output, stdout)
                stderr = self._output(execution / "stderr.log")
                opened.callback(self._close_output, stderr)
                self._sync_dir(execution)
                result = subprocess.run(
                    self._start_command(unit, lease, workdir, command, timeout),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                receipt = self._stop_after_timeout(lease_id, unit)
            except OSError as error:
                receipt = {**receipt, "state": "not_started", "error": type(error).__name__}
            else:
                if self._terminal_and_collect(lease, unit):
                    receipt = {
                        **receipt,
                        "state": "completed" if result.returncode == 0 else "failed",
                        "returncode": result.returncode,
                    }
                else:
                    receipt = {**receipt, "state": "unknown", "returncode": result.returncode}
        self.broker.record_execution(lease_id, receipt)
        return receipt

    def _stop_after_timeout(self, lease_id: str, unit: str) -> dict[str, Any]:
        try:
            stopped = subprocess.run(
                ["systemctl", "--user", f"--machine={HOST}", "stop", unit],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=STOP_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {
                "lease_id": lease_id,
                "unit": unit,
                "state": "unknown",
                "error": type(error).__name__,
            }
        if stopped.returncode:
            return {
                "lease_id": lease_id,
                "unit": unit,
                "state": "unknown",
                "stop_returncode": stopped.returncode,
            }
        terminal, empty = ManagedExecutor.runtime_proof(unit)
        reconciled = self.broker.reconcile_execution(
            lease_id,
            reservation_id=self.broker.lease(lease_id)["reservation_id"],
            attempt_id=f"attempt-{lease_id}",
            unit=unit,
            observed_boot_id=self.broker.boot_id,
            execution_started=True,
            unit_terminal=terminal,
            cgroup_empty=empty,
            release_state="cancelled",
        )
        if reconciled["state"] != "released":
            return {"lease_id": lease_id, "unit": unit, "state": "unknown"}
        return {"lease_id": lease_id, "unit": unit, "state": "timed_out"}

    def _terminal_and_collect(self, lease: dict[str, Any], unit: str) -> bool:
        terminal, empty = ManagedExecutor.runtime_proof(unit)
        reconciled = self.broker.reconcile_execution(
            lease["lease_id"],
            reservation_id=lease["reservation_id"],
            attempt_id=f"attempt-{lease['lease_id']}",
            unit=unit,
            observed_boot_id=self.broker.boot_id,
            execution_started=True,
            unit_terminal=terminal,
            cgroup_empty=empty,
        )
        if reconciled["state"] != "released":
            return False
        ManagedExecutor.reset_failed(unit)
        return True

    @staticmethod
    def _start_command(
        unit: str, lease: dict[str, Any], workdir: Path, command: Sequence[str], timeout: int
    ) -> list[str]:
        own = lease["own"]
        properties = [
            f"MemoryHigh={own['memory_high_mib']}M",
            f"MemoryMax={own['memory_max_mib']}M",
            f"MemorySwapMax={own['swap_max_mib']}M",
            f"CPUQuota={own['cpu_percent']}%",
            f"TasksMax={own['tasks']}",
            f"RuntimeMaxSec={timeout}s",
            f"BindReadOnlyPaths={workdir}",
            "KillMode=control-group",
            *SANDBOX,
        ]
        arguments = [
            "systemd-run",
            "--user",
            f"--machine={HOST}",
            "--wait",
            "--pipe",
            "--expand-environment=no",
            "--service-type=exec",
            f"--unit={unit}",
            "--slice=switchstand-agents.slice",
            f"--working-directory={workdir}",
        ]
        arguments.extend(f"--property={value}" for value in properties)
        clean_environment = ["/usr/bin/env", "-i", "HOME=/tmp", "PATH=/usr/bin:/bin", "TMPDIR=/tmp"]
        return [*arguments, "--", *clean_environment, *command]

    @staticmethod
    def _unit(lease_id: str) -> str:
        digest = hashlib.sha256(lease_id.encode()).hexdigest()[:20]
        return f"switchstand-agent-{digest}.service"

    @staticmethod
    def _output(path: Path) -> int:
        return os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )

    @staticmethod
    def _close_output(descriptor: int) -> None:
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _sync_dir(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class ManagedExecutor:
    """Execute only a sealed managed-parent manifest inside its aggregate lease."""

    def __init__(self, broker: Broker | None = None) -> None:
        self.broker = broker or Broker()
        self.manifests = PreparedLaunchStore(self.broker)

    def run(self, manifest_path: Path, timeout: int = RUN_TIMEOUT_SECONDS) -> dict[str, Any]:
        if timeout <= 0:
            raise ValueError("a positive timeout is required")
        manifest = self.manifests.load(manifest_path)
        lease = self.broker.lease(manifest.lease_id)
        if lease.get("reservation_id") != manifest.reservation_id:
            raise ValueError("manifest reservation does not match the active lease")
        execution = self.broker.execution_dir(manifest.lease_id)
        receipt: dict[str, Any] = {
            "launch_id": manifest.launch_id,
            "lease_id": manifest.lease_id,
            "reservation_id": manifest.reservation_id,
            "attempt_id": manifest.attempt_id,
            "unit": manifest.unit,
            "work_id": str(manifest.work_id),
            "grant_id": str(manifest.grant_id),
            "grant_version": manifest.grant_version,
            "state": "starting",
        }
        self.broker.attach_execution(
            manifest.lease_id,
            reservation_id=manifest.reservation_id,
            attempt_id=manifest.attempt_id,
            unit=manifest.unit,
            receipt=receipt,
        )
        with ExitStack() as opened:
            try:
                stdout = self._output(execution / "stdout.log")
                opened.callback(self._close_output, stdout)
                stderr = self._output(execution / "stderr.log")
                opened.callback(self._close_output, stderr)
                self._sync_dir(execution)
                result = subprocess.run(
                    self._start_command(manifest, lease, execution, timeout),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                receipt = self._stop_and_reconcile(manifest, receipt)
            except OSError as error:
                receipt = {**receipt, "state": "not_started", "error": type(error).__name__}
            else:
                receipt = self._finish_and_reconcile(manifest, receipt, result.returncode)
        self.broker.record_execution(manifest.lease_id, receipt)
        return receipt

    def _finish_and_reconcile(
        self, manifest: PreparedLaunch, receipt: dict[str, Any], returncode: int
    ) -> dict[str, Any]:
        reconciliation = self._reconcile(
            manifest, execution_started=True, release_state="completed"
        )
        if reconciliation["state"] != "released":
            return {**receipt, "state": "unknown", "returncode": returncode}
        self.reset_failed(manifest.unit)
        return {
            **receipt,
            "state": "completed" if returncode == 0 else "failed",
            "returncode": returncode,
        }

    def _stop_and_reconcile(
        self, manifest: PreparedLaunch, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            stopped = subprocess.run(
                ["systemctl", "--user", f"--machine={HOST}", "stop", manifest.unit],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=STOP_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {**receipt, "state": "unknown", "error": type(error).__name__}
        if stopped.returncode:
            return {**receipt, "state": "unknown", "stop_returncode": stopped.returncode}
        reconciliation = self._reconcile(
            manifest, execution_started=True, release_state="cancelled"
        )
        if reconciliation["state"] != "released":
            return {**receipt, "state": "unknown"}
        self.reset_failed(manifest.unit)
        return {**receipt, "state": "timed_out"}

    def _reconcile(
        self,
        manifest: PreparedLaunch,
        *,
        execution_started: bool | None,
        release_state: Literal["completed", "cancelled"],
    ) -> dict[str, str]:
        terminal, empty = self.runtime_proof(manifest.unit)
        return self.broker.reconcile_execution(
            manifest.lease_id,
            reservation_id=manifest.reservation_id,
            attempt_id=manifest.attempt_id,
            unit=manifest.unit,
            observed_boot_id=self.broker.boot_id,
            execution_started=execution_started,
            unit_terminal=terminal,
            cgroup_empty=empty,
            release_state=release_state,
        )

    @staticmethod
    def runtime_proof(unit: str) -> tuple[bool | None, bool | None]:
        try:
            values: list[str] = []
            for property_name in ("ActiveState", "ControlGroup"):
                status = subprocess.run(
                    [
                        "systemctl",
                        "--user",
                        f"--machine={HOST}",
                        "show",
                        f"--property={property_name}",
                        "--value",
                        unit,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=STOP_TIMEOUT_SECONDS,
                    text=True,
                )
                if status.returncode:
                    return None, None
                values.append(status.stdout.strip())
        except OSError, subprocess.TimeoutExpired:
            return None, None
        terminal = values[0] in {"inactive", "failed"}
        group = values[1]
        if not group:
            return terminal, True
        events = Path("/sys/fs/cgroup") / group.removeprefix("/") / "cgroup.events"
        try:
            populated = next(
                line.split()[1]
                for line in events.read_text().splitlines()
                if line.startswith("populated ")
            )
        except FileNotFoundError:
            return terminal, True
        except OSError, IndexError, StopIteration:
            return terminal, None
        return terminal, populated == "0"

    @staticmethod
    def reset_failed(unit: str) -> None:
        try:
            subprocess.run(
                ["systemctl", "--user", f"--machine={HOST}", "reset-failed", unit],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=STOP_TIMEOUT_SECONDS,
            )
        except OSError, subprocess.TimeoutExpired:
            pass

    @staticmethod
    def _output(path: Path) -> int:
        return os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )

    @staticmethod
    def _close_output(descriptor: int) -> None:
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _sync_dir(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _start_command(
        manifest: PreparedLaunch,
        lease: dict[str, Any],
        execution: Path,
        timeout: int,
    ) -> list[str]:
        total = lease["total"]
        properties = [
            f"MemoryHigh={total['memory_high_mib']}M",
            f"MemoryMax={total['memory_max_mib']}M",
            f"MemorySwapMax={total['swap_max_mib']}M",
            f"CPUQuota={total['cpu_percent']}%",
            f"TasksMax={total['tasks']}",
            f"RuntimeMaxSec={timeout}s",
            f"BindReadOnlyPaths={manifest.control}",
            f"BindPaths={manifest.writer}",
            f"BindPaths={manifest.codex_home}",
            f"BindPaths={execution}",
            "KillMode=control-group",
            *MANAGED_SANDBOX,
        ]
        arguments = [
            "systemd-run",
            "--user",
            f"--machine={HOST}",
            "--wait",
            "--pipe",
            "--expand-environment=no",
            "--service-type=exec",
            f"--unit={manifest.unit}",
            "--slice=switchstand-agents.slice",
            f"--working-directory={manifest.writer}",
        ]
        arguments.extend(f"--property={value}" for value in properties)
        environment = [
            "/usr/bin/env",
            "-i",
            f"HOME={manifest.codex_home}",
            f"CODEX_HOME={manifest.codex_home}",
            "PATH=/usr/bin:/bin",
            "TMPDIR=/tmp",
            f"ACTIVE_WORK_ID={manifest.work_id}",
            "SWITCHSTAND_MANAGED=1",
            f"SWITCHSTAND_GRANT_ID={manifest.grant_id}",
            f"SWITCHSTAND_GRANT_VERSION={manifest.grant_version}",
            f"SWITCHSTAND_TASK_WRITER={manifest.writer}",
            f"SWITCHSTAND_TASK_ID={manifest.work_id}",
        ]
        return [*arguments, "--", *environment, *manifest.command]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lease_id")
    parser.add_argument("working_directory", type=Path)
    parser.add_argument("--timeout", type=int, default=RUN_TIMEOUT_SECONDS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    print(
        json.dumps(
            Executor().run(args.lease_id, args.command, args.working_directory, args.timeout)
        )
    )
