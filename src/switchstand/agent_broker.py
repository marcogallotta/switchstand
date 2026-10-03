"""Inert, host-wide resource lease broker for recursive agent workers."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator

STATE_ROOT = Path.home() / ".local/state/switchstand/agent-broker"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
ACTIVE = {"reserved", "execution_active"}
FIELDS = ("memory_high_mib", "memory_max_mib", "swap_max_mib", "cpu_percent", "tasks")
PRESSURE_MAX_AGE_SECONDS = 5.0
SWAP_ACTIVITY_MAX_WINDOW_SECONDS = 60.0
CLAIM_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class Budget:
    memory_high_mib: int
    memory_max_mib: int
    swap_max_mib: int
    cpu_percent: int
    tasks: int
    workers: int
    heavy: int

    def __post_init__(self) -> None:
        values = asdict(self)
        if any(type(value) is not int or value < 0 for value in values.values()):
            raise ValueError("budget values must be non-negative integers")
        if self.memory_high_mib > self.memory_max_mib:
            raise ValueError("memory_high_mib exceeds memory_max_mib")

    def add(self, other: Budget) -> Budget:
        return Budget(**{key: getattr(self, key) + getattr(other, key) for key in asdict(self)})

    def fits(self, limit: Budget) -> bool:
        return all(getattr(self, key) <= getattr(limit, key) for key in asdict(self))


ZERO = Budget(0, 0, 0, 0, 0, 0, 0)
CLASSES = {
    "light": Budget(700, 1024, 128, 100, 96, 1, 0),
    "implementation": Budget(1229, 1792, 256, 200, 160, 1, 0),
    "heavy": Budget(2560, 3584, 512, 400, 384, 1, 1),
}


class ChildBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    memory_high_mib: StrictInt
    memory_max_mib: StrictInt
    swap_max_mib: StrictInt
    cpu_percent: StrictInt
    tasks: StrictInt
    workers: StrictInt
    heavy: StrictInt

    @field_validator("*", mode="after")
    @classmethod
    def non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("budget values must be non-negative")
        return value

    def budget(self) -> Budget:
        return Budget(**self.model_dump())


class LeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: StrictStr
    parent: StrictStr
    worker: StrictStr
    worker_class: StrictStr
    children: ChildBudget

    @field_validator("request_id", "parent", "worker")
    @classmethod
    def safe_id(cls, value: str) -> str:
        if not ID.fullmatch(value):
            raise ValueError("invalid identifier")
        return value

    @field_validator("worker_class")
    @classmethod
    def known_class(cls, value: str) -> str:
        if value not in CLASSES:
            raise ValueError("unknown worker class")
        return value


@dataclass(frozen=True)
class Pressure:
    memory_total_mib: int
    memory_available_mib: int
    swap_total_mib: int
    swap_used_mib: int
    memory_psi_avg10: float
    swap_activity_pages: int | None = None
    sampled_monotonic: float | None = None
    activity_window_seconds: float | None = None
    pswpin: int | None = None
    pswpout: int | None = None

    @classmethod
    def current(cls) -> Pressure:
        memory: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value, *_ = line.replace(":", "").split()
            memory[key] = int(value) // 1024
        psi = Path("/proc/pressure/memory").read_text().splitlines()[0]
        avg10 = float(next(item.split("=")[1] for item in psi.split() if item.startswith("avg10=")))
        vmstat: dict[str, int] = {}
        for line in Path("/proc/vmstat").read_text().splitlines():
            key, value = line.split()
            if key in {"pswpin", "pswpout"}:
                vmstat[key] = int(value)
        if set(vmstat) != {"pswpin", "pswpout"}:
            raise ValueError("missing swap activity counters")
        return cls(
            memory["MemTotal"],
            memory["MemAvailable"],
            memory["SwapTotal"],
            memory["SwapTotal"] - memory["SwapFree"],
            avg10,
            sampled_monotonic=time.monotonic(),
            pswpin=vmstat["pswpin"],
            pswpout=vmstat["pswpout"],
        )

    def with_activity(self, previous: Mapping[str, Any] | None) -> Pressure:
        """Derive recent swap movement from a prior sample from this broker ledger."""
        if self.pswpin is None or self.pswpout is None or self.sampled_monotonic is None:
            return self
        if previous is None:
            return self
        try:
            elapsed = self.sampled_monotonic - float(previous["sampled_monotonic"])
            activity = (self.pswpin - int(previous["pswpin"])) + (
                self.pswpout - int(previous["pswpout"])
            )
        except KeyError, TypeError, ValueError:
            return self
        if elapsed <= 0 or activity < 0:
            return self
        return Pressure(
            self.memory_total_mib,
            self.memory_available_mib,
            self.swap_total_mib,
            self.swap_used_mib,
            self.memory_psi_avg10,
            swap_activity_pages=activity,
            sampled_monotonic=self.sampled_monotonic,
            activity_window_seconds=elapsed,
            pswpin=self.pswpin,
            pswpout=self.pswpout,
        )

    def sample_record(self) -> dict[str, int | float] | None:
        if self.pswpin is None or self.pswpout is None or self.sampled_monotonic is None:
            return None
        return {
            "pswpin": self.pswpin,
            "pswpout": self.pswpout,
            "sampled_monotonic": self.sampled_monotonic,
        }

    def refusal(
        self,
        worker_class: str,
        *,
        reserved_memory_mib: int = 0,
        candidate_memory_mib: int = 0,
        now_monotonic: float | None = None,
    ) -> str | None:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        numbers = (
            self.memory_total_mib,
            self.memory_available_mib,
            self.swap_total_mib,
            self.swap_used_mib,
            self.memory_psi_avg10,
            reserved_memory_mib,
            candidate_memory_mib,
        )
        if any(not math.isfinite(value) or value < 0 for value in numbers):
            return "pressure_sample_invalid"
        sampled = self.sampled_monotonic
        if sampled is not None and (not math.isfinite(sampled) or now < sampled):
            return "pressure_sample_invalid"
        if sampled is not None and now - sampled > PRESSURE_MAX_AGE_SECONDS:
            return "pressure_sample_stale"
        activity = self.swap_activity_pages
        # Old explicit fixtures with no swap usage represent a fresh zero-activity sample.
        # A real sample always carries cumulative vmstat counters and is derived by the broker.
        if (
            activity is None
            and self.pswpin is None
            and self.pswpout is None
            and not self.swap_used_mib
        ):
            activity = 0
        if activity is None:
            if self.pswpin is None and self.pswpout is None and self.swap_used_mib:
                # Preserve the conservative meaning of legacy injected red samples. Live
                # admission uses vmstat deltas; an explicit zero delta admits used swap.
                return "host_pressure"
            return "pressure_sample_missing"
        if type(activity) is not int or activity < 0:
            return "pressure_sample_invalid"
        if self.activity_window_seconds is not None:
            if not math.isfinite(self.activity_window_seconds) or self.activity_window_seconds <= 0:
                return "pressure_sample_invalid"
            if self.activity_window_seconds > SWAP_ACTIVITY_MAX_WINDOW_SECONDS:
                return "pressure_sample_stale"
        required = (
            max(2048, self.memory_total_mib // 5) + reserved_memory_mib + candidate_memory_mib
        )
        if self.memory_available_mib < required:
            return "memory_available"
        if activity or self.memory_psi_avg10 >= 5:
            return "host_pressure"
        if worker_class != "light" and self.memory_psi_avg10 >= 2:
            return "host_pressure_light_only"
        return None


class Broker:
    def __init__(
        self,
        root: Path = STATE_ROOT,
        *,
        clock: Callable[[], float] = time.monotonic,
        boot_id: str | None = None,
    ) -> None:
        self.root = root
        self.state_path = root / "state.json"
        self.lock_path = root / "broker.lock"
        self.clock = clock
        self.boot_id = boot_id or Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def initialize(self, budget: Budget) -> None:
        self._secure_dir(self.root)
        self._secure_dir(self.root / "inboxes")
        self._secure_dir(self.root / "inboxes" / "root")
        self._secure_dir(self.root / "results")
        with self._locked():
            if self.state_path.exists():
                raise RuntimeError("broker is already initialized")
            state = {
                "version": 2,
                "root": asdict(budget),
                "leases": {},
                "requests": {},
                "pressure_sample": None,
            }
            self._atomic_json(self.state_path, state)

    def ingest(
        self, parent: str, request_id: str, pressure: Pressure | None = None
    ) -> dict[str, Any]:
        if not ID.fullmatch(parent) or not ID.fullmatch(request_id):
            raise ValueError("invalid identifier")
        request = LeaseRequest.model_validate_json(self._read_request(parent, request_id))
        if request.parent != parent or request.request_id != request_id:
            raise ValueError("request identity does not match its inbox")
        with self._locked():
            state = self._read_state()
            prior = state["requests"].get(request_id)
            if prior is not None:
                if prior["parent"] != parent:
                    raise ValueError("request identifier collision")
                self._atomic_json(self.root / "results" / f"{request_id}.json", prior)
                return prior
            sample = self._pressure(state, pressure)
            reserved_memory, candidate_memory = self._admission_memory(state, request)
            refusal = sample.refusal(
                request.worker_class,
                reserved_memory_mib=reserved_memory,
                candidate_memory_mib=candidate_memory,
                now_monotonic=self.clock(),
            )
            if refusal:
                result = {
                    "request_id": request_id,
                    "parent": parent,
                    "state": "refused",
                    "reason": refusal,
                }
            else:
                result = self._reserve(state, request)
            state["requests"][request_id] = result
            self._atomic_json(self.state_path, state)
            self._atomic_json(self.root / "results" / f"{request_id}.json", result)
            return result

    def complete(self, lease_id: str) -> None:
        with self._locked():
            state = self._read_state()
            lease = self._active_lease(state, lease_id)
            if lease["state"] != "reserved":
                raise RuntimeError("execution completion requires executor proof")
            if any(
                item["state"] in ACTIVE and item["parent"] == lease_id
                for item in state["leases"].values()
            ):
                raise RuntimeError("lease has active children")
            lease["state"] = "completed"
            self._atomic_json(self.state_path, state)

    def cancel(self, lease_id: str) -> list[str]:
        with self._locked():
            state = self._read_state()
            self._active_lease(state, lease_id)
            subtree: list[str] = []
            pending = [lease_id]
            while pending:
                current = pending.pop()
                subtree.append(current)
                pending.extend(
                    key
                    for key, item in state["leases"].items()
                    if item["parent"] == current and item["state"] in ACTIVE
                )
            if any(state["leases"][item]["state"] == "execution_active" for item in subtree):
                raise RuntimeError("active execution requires confirmed stop")
            for item in subtree:
                state["leases"][item]["state"] = "cancelled"
            self._atomic_json(self.state_path, state)
            return subtree

    def status(self) -> dict[str, Any]:
        with self._locked():
            return self._read_state()

    def lease(self, lease_id: str) -> dict[str, Any]:
        with self._locked():
            return deepcopy(self._active_lease(self._read_state(), lease_id))

    def execution_dir(self, lease_id: str) -> Path:
        if not ID.fullmatch(lease_id):
            raise ValueError("invalid identifier")
        executions = self.root / "executions"
        self._secure_dir(executions)
        path = executions / lease_id
        self._secure_dir(path)
        return path

    def record_execution(self, lease_id: str, receipt: Mapping[str, Any]) -> None:
        with self._locked():
            self._atomic_json(self.execution_dir(lease_id) / "status.json", receipt)

    def record_canary(self, run_id: str, receipt: Mapping[str, Any]) -> Path:
        if not ID.fullmatch(run_id):
            raise ValueError("invalid identifier")
        with self._locked():
            canaries = self.root / "canaries"
            self._secure_dir(canaries)
            directory = canaries / run_id
            self._secure_dir(directory)
            path = directory / "report.json"
            self._atomic_json(path, receipt)
            return path

    def attach_execution(
        self,
        lease_id: str,
        *,
        reservation_id: str,
        attempt_id: str,
        unit: str,
        receipt: Mapping[str, Any],
        claim_ttl_seconds: float = CLAIM_TTL_SECONDS,
    ) -> None:
        """Atomically bind one executor attempt and systemd unit to a reservation."""
        if not ID.fullmatch(attempt_id) or not ID.fullmatch(unit):
            raise ValueError("invalid attempt or unit identifier")
        if not math.isfinite(claim_ttl_seconds) or claim_ttl_seconds <= 0:
            raise ValueError("claim ttl must be positive")
        for key, expected in (("attempt_id", attempt_id), ("unit", unit)):
            if key in receipt and receipt[key] != expected:
                raise ValueError(f"receipt {key} does not match execution claim")
        with self._locked():
            state = self._read_state()
            lease = self._active_lease(state, lease_id)
            if lease["state"] != "reserved":
                raise RuntimeError("lease execution is already claimed")
            if lease["reservation_id"] != reservation_id:
                raise ValueError("reservation identity mismatch")
            lease["state"] = "execution_active"
            lease["attempt_id"] = attempt_id
            lease["unit"] = unit
            lease["claim_expires_monotonic"] = self.clock() + claim_ttl_seconds
            lease["boot_id"] = self.boot_id
            self._atomic_json(self.state_path, state)
            path = self.execution_dir(lease_id) / "status.json"
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "w") as handle:
                json.dump(receipt, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_dir(path.parent)

    def claim_execution(self, lease_id: str, receipt: Mapping[str, Any]) -> None:
        """Compatibility wrapper; new executors should use attach_execution explicitly."""
        lease = self.lease(lease_id)
        attempt_id = receipt.get("attempt_id", f"attempt-{lease_id}")
        unit = receipt.get("unit", f"unknown-{lease_id}.service")
        if not isinstance(attempt_id, str) or not isinstance(unit, str):
            raise TypeError("attempt and unit identifiers must be strings")
        self.attach_execution(
            lease_id,
            reservation_id=lease["reservation_id"],
            attempt_id=attempt_id,
            unit=unit,
            receipt=receipt,
        )

    def reconcile_execution(
        self,
        lease_id: str,
        *,
        reservation_id: str,
        attempt_id: str,
        unit: str,
        observed_boot_id: str,
        execution_started: bool | None,
        unit_terminal: bool | None,
        cgroup_empty: bool | None,
        observed_monotonic: float | None = None,
        release_state: Literal["completed", "cancelled"] = "completed",
    ) -> dict[str, str]:
        """Release a crashed attempt only from positive, identity-bound runtime proof."""
        observed = self.clock() if observed_monotonic is None else observed_monotonic
        with self._locked():
            state = self._read_state()
            lease = self._active_lease(state, lease_id)
            if (
                lease.get("reservation_id") != reservation_id
                or lease.get("attempt_id") != attempt_id
                or lease.get("unit") != unit
            ):
                raise ValueError("execution identity mismatch")
            if observed_boot_id != lease.get("boot_id") or observed_boot_id != self.boot_id:
                return {"state": "unknown", "reason": "boot_identity"}
            resolved_state: str | None = None
            if execution_started is False:
                expires = lease.get("claim_expires_monotonic")
                if isinstance(expires, (int, float)) and observed >= expires:
                    resolved_state = "abandoned"
            if resolved_state is None and unit_terminal is True and cgroup_empty is True:
                resolved_state = release_state
            if resolved_state is None:
                return {"state": "unknown", "reason": "runtime_ambiguous"}
            if any(
                item["state"] in ACTIVE and item["parent"] == lease_id
                for item in state["leases"].values()
            ):
                return {"state": "unknown", "reason": "active_children"}
            lease["state"] = resolved_state
            lease["reconciled_monotonic"] = observed
            self._atomic_json(self.state_path, state)
            return {"state": "released", "reason": resolved_state}

    def reconcile_reservation(
        self,
        lease_id: str,
        *,
        reservation_id: str,
        observed_boot_id: str,
        launch_absent: bool | None,
        observed_monotonic: float | None = None,
        reservation_ttl_seconds: float = CLAIM_TTL_SECONDS,
    ) -> dict[str, str]:
        """Release an unattached reservation only after expiry and positive absence proof."""
        observed = self.clock() if observed_monotonic is None else observed_monotonic
        with self._locked():
            state = self._read_state()
            lease = self._active_lease(state, lease_id)
            if lease.get("reservation_id") != reservation_id:
                raise ValueError("reservation identity mismatch")
            if lease.get("state") != "reserved":
                return {"state": "unknown", "reason": "execution_attached"}
            if observed_boot_id != lease.get("boot_id") or observed_boot_id != self.boot_id:
                return {"state": "unknown", "reason": "boot_identity"}
            reserved = lease.get("reserved_monotonic")
            if (
                launch_absent is not True
                or not isinstance(reserved, (int, float))
                or observed < reserved + reservation_ttl_seconds
            ):
                return {"state": "unknown", "reason": "launch_ambiguous"}
            lease["state"] = "abandoned"
            lease["reconciled_monotonic"] = observed
            self._atomic_json(self.state_path, state)
            return {"state": "released", "reason": "abandoned"}

    def finish_execution(self, lease_id: str, state_name: str) -> None:
        """Reject the legacy proof-free release path."""
        del lease_id, state_name
        raise RuntimeError("execution release requires identity-bound runtime reconciliation")

    def _reserve(self, state: dict[str, Any], request: LeaseRequest) -> dict[str, Any]:
        if request.worker == "root" or request.worker in state["leases"]:
            raise ValueError("worker identifier already exists")
        own = CLASSES[request.worker_class]
        children = request.children.budget()
        requested = own.add(children)
        if request.parent == "root":
            limit = Budget(**state["root"])
        else:
            parent = self._active_lease(state, request.parent)
            limit = Budget(**parent["children"])
        used = ZERO
        for item in state["leases"].values():
            if item["state"] in ACTIVE and item["parent"] == request.parent:
                used = used.add(Budget(**item["total"]))
        if not used.add(requested).fits(limit):
            return {
                "request_id": request.request_id,
                "parent": request.parent,
                "state": "refused",
                "reason": "parent_budget",
            }
        lease = {
            "lease_id": request.worker,
            "reservation_id": uuid.uuid4().hex,
            "request_id": request.request_id,
            "parent": request.parent,
            "worker_class": request.worker_class,
            "own": asdict(own),
            "children": asdict(children),
            "total": asdict(requested),
            "state": "reserved",
            "reserved_monotonic": self.clock(),
            "boot_id": self.boot_id,
            "attempt_id": None,
            "unit": None,
        }
        state["leases"][request.worker] = lease
        self._secure_dir(self.root / "inboxes" / request.worker)
        return {
            "request_id": request.request_id,
            "parent": request.parent,
            "state": "reserved",
            "lease_id": request.worker,
            "reservation_id": lease["reservation_id"],
        }

    def _pressure(self, state: dict[str, Any], supplied: Pressure | None) -> Pressure:
        if supplied is not None:
            return supplied
        try:
            raw = Pressure.current()
        except OSError, ValueError, KeyError, StopIteration:
            return Pressure(0, 0, 0, 0, math.nan)
        pressure = raw.with_activity(state.get("pressure_sample"))
        record = raw.sample_record()
        if record is not None:
            state["pressure_sample"] = record
        return pressure

    @staticmethod
    def _admission_memory(state: dict[str, Any], request: LeaseRequest) -> tuple[int, int]:
        reserved = sum(
            int(item["total"]["memory_max_mib"])
            for item in state["leases"].values()
            if item["state"] in ACTIVE and item["parent"] == "root"
        )
        if request.parent != "root":
            return reserved, 0
        requested = CLASSES[request.worker_class].add(request.children.budget())
        return reserved, requested.memory_max_mib

    def _read_request(self, parent: str, request_id: str) -> bytes:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        with ExitStack() as opened:
            directory: int | None = None
            for component in (self.root, Path("inboxes"), Path(parent)):
                directory = os.open(component, directory_flags, dir_fd=directory)
                opened.callback(os.close, directory)
                info = os.fstat(directory)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise PermissionError("unsafe inbox directory")
            descriptor = os.open(
                f"{request_id}.json",
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            opened.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise PermissionError("unsafe request file")
            if info.st_mode & 0o022 or info.st_size > 16_384:
                raise PermissionError("unsafe request permissions or size")
            return os.read(descriptor, 16_385)

    @contextmanager
    def _locked(self) -> Generator[None]:
        self._secure_dir(self.root)
        descriptor = os.open(
            self.lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _read_state(self) -> dict[str, Any]:
        descriptor = os.open(self.state_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            return json.loads(os.read(descriptor, 4_000_000))
        finally:
            os.close(descriptor)

    @staticmethod
    def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                json.dump(value, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            Broker._fsync_dir(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _secure_dir(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError(f"unsafe broker directory: {path}")

    @staticmethod
    def _active_lease(state: dict[str, Any], lease_id: str) -> dict[str, Any]:
        lease = state["leases"].get(lease_id)
        if lease is None or lease["state"] not in ACTIVE:
            raise ValueError("lease is not active")
        return lease


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    for field in (*FIELDS, "workers", "heavy"):
        initialize.add_argument(f"--{field.replace('_', '-')}", type=int, required=True)
    ingest = commands.add_parser("ingest")
    ingest.add_argument("parent")
    ingest.add_argument("request_id")
    for command in (commands.add_parser("complete"), commands.add_parser("cancel")):
        command.add_argument("lease_id")
    commands.add_parser("status")
    args = parser.parse_args()
    broker = Broker()
    if args.command == "init":
        broker.initialize(
            Budget(**{key: getattr(args, key) for key in (*FIELDS, "workers", "heavy")})
        )
        output: Any = {"state": "initialized"}
    elif args.command == "ingest":
        output = broker.ingest(args.parent, args.request_id)
    elif args.command == "complete":
        broker.complete(args.lease_id)
        output = {"state": "completed", "lease_id": args.lease_id}
    elif args.command == "cancel":
        output = {"state": "cancelled", "leases": broker.cancel(args.lease_id)}
    else:
        output = broker.status()
    print(json.dumps(output, sort_keys=True))
