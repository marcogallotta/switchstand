"""Read-only proof executed inside a resource-governed worker."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

DOCKER_SOCKETS = (Path("/run/docker.sock"), Path("/var/run/docker.sock"))
CGROUP_FILES = ("memory.high", "memory.max", "memory.swap.max", "cpu.max", "pids.max")


def probe(
    relative_path: str,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    cgroup_relative: str | None = None,
) -> dict[str, object]:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("probe path must remain inside the read-only workdir")
    payload = candidate.read_bytes()
    exposed: list[str] = []
    for socket in DOCKER_SOCKETS:
        try:
            os.stat(socket)
        except (FileNotFoundError, PermissionError):
            continue
        exposed.append(str(socket))
    if exposed:
        raise RuntimeError(f"Docker socket visible: {exposed}")
    if cgroup_relative is None:
        unified = next(
            line for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::")
        )
        cgroup_relative = unified.partition("::")[2].lstrip("/")
    cgroup = cgroup_root / cgroup_relative
    return {
        "path": relative_path,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "docker_socket": "blocked",
        "cgroup": {name: (cgroup / name).read_text().strip() for name in CGROUP_FILES},
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("relative_path")
    print(json.dumps(probe(parser.parse_args().relative_path), sort_keys=True))


if __name__ == "__main__":
    main()
