"""Current-user POSIX persistence primitives for the legacy engine."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Mapping


PERSISTENCE_ERROR = "Switchstand persistence path is unsafe"


def persistence_flags(*names: str) -> int:
    flags = 0
    for name in names:
        value = getattr(os, name, None)
        if not isinstance(value, int):
            raise RuntimeError(PERSISTENCE_ERROR)
        flags |= value
    cloexec = getattr(os, "O_CLOEXEC", 0)
    return flags | (cloexec if isinstance(cloexec, int) else 0)


def require_posix_persistence() -> None:
    required = ("geteuid", "fstat", "fchmod", "fsync")
    if os.name != "posix" or not all(callable(getattr(os, name, None)) for name in required):
        raise RuntimeError(PERSISTENCE_ERROR)
    persistence_flags("O_NOFOLLOW", "O_NONBLOCK", "O_DIRECTORY")


def private_fd(path: Path, flags: int) -> int:
    try:
        fd = os.open(path, flags | persistence_flags("O_NOFOLLOW", "O_NONBLOCK"), 0o600)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RuntimeError(PERSISTENCE_ERROR) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise RuntimeError(PERSISTENCE_ERROR)
        os.fchmod(fd, 0o600)
    except Exception as exc:
        os.close(fd)
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(PERSISTENCE_ERROR) from exc
    return fd


def read_private_json(path: Path) -> Any:
    with os.fdopen(private_fd(path, os.O_RDONLY), "r", encoding="utf-8") as handle:
        return json.load(handle)


def append_private_json(path: Path, value: Mapping[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    with os.fdopen(private_fd(path, flags), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    require_posix_persistence()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | persistence_flags("O_DIRECTORY"))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp.exists():
            tmp.unlink()
