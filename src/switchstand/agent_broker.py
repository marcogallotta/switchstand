"""Inert, host-wide resource lease broker for recursive agent workers."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import tempfile
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator

STATE_ROOT = Path.home() / ".local/state/switchstand/agent-broker"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
ACTIVE = {"reserved"}
FIELDS = ("memory_high_mib", "memory_max_mib", "swap_max_mib", "cpu_percent", "tasks")


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

    @classmethod
    def current(cls) -> Pressure:
        memory: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value, *_ = line.replace(":", "").split()
            memory[key] = int(value) // 1024
        psi = Path("/proc/pressure/memory").read_text().splitlines()[0]
        avg10 = float(next(item.split("=")[1] for item in psi.split() if item.startswith("avg10=")))
        return cls(
            memory["MemTotal"],
            memory["MemAvailable"],
            memory["SwapTotal"],
            memory["SwapTotal"] - memory["SwapFree"],
            avg10,
        )

    def refusal(self, worker_class: str) -> str | None:
        if self.memory_available_mib < max(2048, self.memory_total_mib // 5):
            return "memory_available"
        swap = 0 if not self.swap_total_mib else 100 * self.swap_used_mib / self.swap_total_mib
        if swap >= 50 or self.memory_psi_avg10 >= 5:
            return "host_pressure"
        if worker_class != "light" and (swap >= 25 or self.memory_psi_avg10 >= 2):
            return "host_pressure_light_only"
        return None


class Broker:
    def __init__(self, root: Path = STATE_ROOT) -> None:
        self.root = root
        self.state_path = root / "state.json"
        self.lock_path = root / "broker.lock"

    def initialize(self, budget: Budget) -> None:
        self._secure_dir(self.root)
        self._secure_dir(self.root / "inboxes")
        self._secure_dir(self.root / "inboxes" / "root")
        self._secure_dir(self.root / "results")
        with self._locked():
            if self.state_path.exists():
                raise RuntimeError("broker is already initialized")
            state = {"version": 1, "root": asdict(budget), "leases": {}, "requests": {}}
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
            refusal = (pressure or Pressure.current()).refusal(request.worker_class)
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
            cancelled: list[str] = []
            pending = [lease_id]
            while pending:
                current = pending.pop()
                pending.extend(
                    key
                    for key, item in state["leases"].items()
                    if item["parent"] == current and item["state"] in ACTIVE
                )
                if state["leases"][current]["state"] in ACTIVE:
                    state["leases"][current]["state"] = "cancelled"
                    cancelled.append(current)
            self._atomic_json(self.state_path, state)
            return cancelled

    def status(self) -> dict[str, Any]:
        with self._locked():
            return self._read_state()

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
            "request_id": request.request_id,
            "parent": request.parent,
            "worker_class": request.worker_class,
            "own": asdict(own),
            "children": asdict(children),
            "total": asdict(requested),
            "state": "reserved",
        }
        state["leases"][request.worker] = lease
        self._secure_dir(self.root / "inboxes" / request.worker)
        return {
            "request_id": request.request_id,
            "parent": request.parent,
            "state": "reserved",
            "lease_id": request.worker,
        }

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
