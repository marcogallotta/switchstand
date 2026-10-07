"""Asynchronous Coordinator handoff to the existing isolated Worker launcher."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from argparse import ArgumentParser
from pathlib import Path
from typing import Literal
from uuid import UUID

from .coordinator_worker_state import (
    COMMAND_TIMEOUT_SECONDS,
    UnitState,
    WorkerCancelResult,
    WorkerRecord,
    WorkerSpawnResult,
    WorkerStatusResult,
    WorkerStore,
)
from .run import stop_receipt

MAX_OBJECTIVE_BYTES = 8_000
HOST = "marco@.host"


class CoordinatorWorkers:
    """Start exact work through the existing isolated launcher in a durable host unit."""

    def __init__(
        self,
        home: Path,
        *,
        launcher: tuple[str, ...] | None = None,
        state_root: Path | None = None,
        host: str = HOST,
        runtime_source: Path | None = None,
    ) -> None:
        self.store = WorkerStore(home, state_root)
        self.home = self.store.home
        self.repo = self.store.repo
        self.launcher = launcher or (str(self.repo / "scripts/switchstand"), "--isolated")
        self.host = host
        self.runtime_source = runtime_source or self.repo / "src"

    def spawn(self, operation_id: UUID, work_id: UUID, objective: str) -> WorkerSpawnResult:
        if not objective.strip() or len(objective.encode()) > MAX_OBJECTIVE_BYTES:
            return WorkerSpawnResult(status="denied", work_id=work_id, reason="objective_invalid")
        try:
            with self.store.locked():
                return self._spawn_locked(operation_id, work_id, objective)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            return WorkerSpawnResult(status="unknown", work_id=work_id, reason=type(error).__name__)

    def _spawn_locked(self, operation_id: UUID, work_id: UUID, objective: str) -> WorkerSpawnResult:
        existing = self.store.load(operation_id)
        if existing is not None:
            if existing.work_id != work_id or existing.objective != objective:
                return WorkerSpawnResult(
                    status="denied", work_id=work_id, reason="operation_identity_conflict"
                )
            return self._spawn_result("replayed", existing)
        self._require_unambiguous_state(work_id)
        base, writer, branch, git_common = self.store.candidate_identity(work_id)
        unit = f"switchstand-implementation-{operation_id.hex}.service"
        log = self.store.root / f"{operation_id}.log"
        descriptor = os.open(
            log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        os.close(descriptor)
        record = WorkerRecord(
            spawn_id=operation_id,
            work_id=work_id,
            objective=objective,
            writer=str(writer),
            branch=branch,
            base_sha=base,
            git_common=git_common,
            unit=unit,
            log=str(log),
            started_at=time.time(),
            command=(
                *self.launcher,
                "--active",
                str(work_id),
                "--commit",
                base,
                "--noninteractive",
                self._prompt(work_id, objective),
            ),
        )
        self.store.write(record)
        result = self._command(
            "systemd-run",
            "--user",
            f"--machine={self.host}",
            "--quiet",
            "--no-block",
            "--service-type=exec",
            f"--unit={unit}",
            f"--working-directory={self.repo}",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=15s",
            "--property=RuntimeMaxSec=3600s",
            f"--property=StandardOutput=append:{log}",
            f"--property=StandardError=append:{log}",
            "--",
            "/usr/bin/env",
            "-i",
            f"HOME={self.home}",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "LC_ALL=C.UTF-8",
            f"PYTHONPATH={self.runtime_source}",
            sys.executable,
            "-P",
            "-m",
            "switchstand.coordinator_workers",
            "--execute",
            str(self.store.root),
            str(operation_id),
            check=False,
        )
        if result.returncode:
            state = self._unit_state(record)
            if state is None or state.load == "not-found":
                return WorkerSpawnResult(
                    status="unknown",
                    spawn_id=operation_id,
                    work_id=work_id,
                    writer=str(writer),
                    base_sha=base,
                    reason="unit_start_not_applied",
                )
        return self._spawn_result("started", record)

    def status(self, spawn_id: UUID) -> WorkerStatusResult:
        try:
            record = self.store.load(spawn_id)
            if record is None:
                return WorkerStatusResult(
                    status="unknown", spawn_id=spawn_id, reason="spawn_not_found"
                )
            if record.phase == "TERMINAL":
                return self._terminal_status(record)
            state = self._unit_state(record)
            if state is None:
                return self._status(record, "unknown", "unit_state_unavailable")
            if state.load == "not-found":
                reason = (
                    "unit_start_not_applied" if record.phase == "PREPARED" else "execution_lost"
                )
                status: Literal["failed", "unknown"] = (
                    "failed" if record.phase == "PREPARED" else "unknown"
                )
                return self._status(record, status, reason)
            if self._unit_running(state):
                return self._status(record, "running", "worker_running")
            return self._status(record, "unknown", "execution_lost")
        except OSError, subprocess.SubprocessError, ValueError:
            return WorkerStatusResult(
                status="unknown", spawn_id=spawn_id, reason="status_unavailable"
            )

    def cancel(self, spawn_id: UUID) -> WorkerCancelResult:
        try:
            record = self.store.load(spawn_id)
            if record is None:
                return WorkerCancelResult(
                    status="unknown", spawn_id=spawn_id, reason="spawn_not_found"
                )
            if record.phase == "TERMINAL":
                return self._cancel_result("already_terminal", spawn_id, "worker_already_terminal")
            state = self._unit_state(record)
            if state is None:
                return self._cancel_result("unknown", spawn_id, "unit_state_unavailable")
            receipt = self.store.run_receipt(record)
            if receipt is not None:
                stopped = stop_receipt(receipt, Path(record.writer), record.branch)
                if stopped.status not in {"stopped", "lost"}:
                    return self._cancel_result("unknown", spawn_id, "managed_run_stop_unconfirmed")
            if state.load == "not-found" or state.active in {"inactive", "failed"}:
                return self._mark_cancelled(record)
            stopped = self._command(
                "systemctl", "--user", f"--machine={self.host}", "stop", record.unit, check=False
            )
            if stopped.returncode:
                current = self.store.load(spawn_id)
                if current is not None and current.phase == "TERMINAL":
                    return self._cancel_result(
                        "already_terminal", spawn_id, "worker_already_terminal"
                    )
                return self._cancel_result("unknown", spawn_id, "unit_stop_failed")
            observed = self._unit_state(record)
            if observed is None or observed.active not in {"inactive", "failed"}:
                return self._cancel_result("unknown", spawn_id, "unit_stop_unconfirmed")
            return self._mark_cancelled(record)
        except OSError, subprocess.SubprocessError, ValueError:
            return self._cancel_result("unknown", spawn_id, "cancellation_unavailable")

    def _mark_cancelled(self, record: WorkerRecord) -> WorkerCancelResult:
        try:
            with self.store.locked():
                current = self.store.load(record.spawn_id)
                if current is None:
                    return self._cancel_result("unknown", record.spawn_id, "worker_record_lost")
                if current.phase == "TERMINAL":
                    return self._cancel_result(
                        "already_terminal", record.spawn_id, "worker_already_terminal"
                    )
                self.store.write(
                    current.model_copy(
                        update={"cancelled": True, "phase": "TERMINAL", "exit_status": -15}
                    ),
                    replace=True,
                )
            return self._cancel_result(
                "cancelled", record.spawn_id, "worker_stopped_writer_preserved"
            )
        except OSError, ValueError:
            return self._cancel_result("unknown", record.spawn_id, "cancellation_unavailable")

    def _require_unambiguous_state(self, work_id: UUID) -> None:
        for record in self.store.records():
            if record.work_id == work_id and record.phase != "TERMINAL":
                state = self._unit_state(record)
                if state is None or self._unit_running(state) or record.phase == "RUNNING":
                    raise ValueError("work_already_has_active_worker")

    def execute(self, spawn_id: UUID) -> int:
        """Systemd-owned wrapper that durably records launcher start and exit."""
        with self.store.locked():
            record = self.store.load(spawn_id)
            if record is None:
                return 2
            if record.phase == "TERMINAL":
                return record.exit_status or 0
            self.store.write(record.model_copy(update={"phase": "RUNNING"}), replace=True)
        try:
            result = subprocess.run(
                list(record.command),
                cwd=self.repo,
                env=self._worker_environment(),
                stdin=subprocess.DEVNULL,
                check=False,
            )
            exit_status = result.returncode
        except OSError:
            exit_status = 127
        with self.store.locked():
            current = self.store.load(spawn_id)
            if current is None:
                return 2
            if current.phase != "TERMINAL":
                self.store.write(
                    current.model_copy(update={"phase": "TERMINAL", "exit_status": exit_status}),
                    replace=True,
                )
        return exit_status

    def _terminal_status(self, record: WorkerRecord) -> WorkerStatusResult:
        candidate, clean, descendant = self.store.candidate(record)
        if record.cancelled:
            return self._status(
                record, "cancelled", "worker_cancelled", candidate if clean and descendant else None
            )
        if record.exit_status != 0:
            return self._status(record, "failed", "launcher_failed")
        if not clean:
            return self._status(record, "failed", "worker_left_uncommitted_changes")
        if candidate is None or not descendant:
            return self._status(record, "failed", "candidate_wrong_ancestry")
        if candidate == record.base_sha:
            return self._status(record, "failed", "worker_returned_no_candidate")
        return self._status(record, "completed", "candidate_ready", candidate)

    def _status(
        self,
        record: WorkerRecord,
        status: Literal["running", "completed", "failed", "cancelled", "unknown"],
        reason: str,
        candidate: str | None = None,
    ) -> WorkerStatusResult:
        summary = None
        if candidate is not None:
            summary = self.store.git(
                Path(record.writer), "show", "-s", "--format=%B", candidate
            ).stdout.strip()[:8_000]
        return WorkerStatusResult(
            status=status,
            spawn_id=record.spawn_id,
            work_id=record.work_id,
            writer=record.writer,
            branch=record.branch,
            base_sha=record.base_sha,
            candidate_sha=candidate,
            summary=summary,
            reason=reason,
        )

    def _unit_state(self, record: WorkerRecord) -> UnitState | None:
        result = self._command(
            "systemctl",
            "--user",
            f"--machine={self.host}",
            "show",
            record.unit,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            check=False,
        )
        if result.returncode:
            missing = f"{result.stdout}\n{result.stderr}".lower()
            if "not found" in missing or "not loaded" in missing:
                return UnitState(load="not-found", active="inactive", sub="dead")
            return None
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines())
        if set(fields) != {"LoadState", "ActiveState", "SubState"}:
            return None
        return UnitState(
            load=fields["LoadState"], active=fields["ActiveState"], sub=fields["SubState"]
        )

    @staticmethod
    def _unit_running(state: UnitState) -> bool:
        return state.active in {"active", "activating", "reloading"} and state.sub not in {
            "dead",
            "failed",
            "exited",
        }

    @staticmethod
    def _spawn_result(
        status: Literal["started", "replayed"], record: WorkerRecord
    ) -> WorkerSpawnResult:
        return WorkerSpawnResult(
            status=status,
            spawn_id=record.spawn_id,
            work_id=record.work_id,
            writer=record.writer,
            base_sha=record.base_sha,
            reason="worker_started" if status == "started" else "operation_replayed",
        )

    @staticmethod
    def _cancel_result(
        status: Literal["cancelled", "already_terminal", "unknown"],
        spawn_id: UUID,
        reason: str,
    ) -> WorkerCancelResult:
        return WorkerCancelResult(status=status, spawn_id=spawn_id, reason=reason)

    @staticmethod
    def _command(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(arguments),
            check=check,
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env={
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C",
                "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
            },
        )

    def _worker_environment(self) -> dict[str, str]:
        codex = self.home / ".local/state/switchstand/codex/updater-bin/bin/codex"
        return {
            "HOME": str(self.home),
            # Activated controls predating SWITCHSTAND_CODEX_BINARY still resolve
            # the literal `codex`; keep that compatibility lookup exact and first.
            "PATH": f"{codex.parent}:/usr/local/bin:/usr/bin:/bin",
            "LC_ALL": "C.UTF-8",
            "SWITCHSTAND_CODEX_BINARY": str(codex),
        }

    @staticmethod
    def _prompt(work_id: UUID, objective: str) -> str:
        return (
            f"Implement exact WorkId {work_id}. Objective:\n{objective}\n\n"
            "Work only in the launch-bound private writer. Run proportionate tests and commit "
            "the complete candidate. End the commit message with `Tests:` and `Remaining:` "
            "lines recording exact PASS/FAIL/NOT_RUN/UNKNOWN evidence. Do not merge, deploy, "
            "activate, or broaden scope."
        )


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("state_root", type=Path)
    parser.add_argument("spawn_id", type=UUID)
    arguments = parser.parse_args()
    if not arguments.execute:
        parser.error("--execute is required")
    return CoordinatorWorkers(Path(os.environ["HOME"]), state_root=arguments.state_root).execute(
        arguments.spawn_id
    )


if __name__ == "__main__":
    raise SystemExit(main())
