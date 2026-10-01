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
from typing import Any

from switchstand.agent_broker import Broker

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
                receipt = {
                    **receipt,
                    "state": "completed" if result.returncode == 0 else "failed",
                    "returncode": result.returncode,
                }
                self.broker.complete(lease_id)
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
        self.broker.cancel(lease_id)
        return {"lease_id": lease_id, "unit": unit, "state": "timed_out"}

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
            "--collect",
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
