import os
import stat
from pathlib import Path

import pytest

from switchstand import secure_file
from switchstand.secure_file import (
    PrivateFileOpenError,
    atomic_replace_bytes,
    create_new_private_bytes,
    read_private_bytes,
)


def test_read_private_bytes_requires_regular_mode_0600_without_following_symlink(
    tmp_path: Path,
) -> None:
    path = tmp_path / "receipt"
    path.write_bytes(b"proof")
    path.chmod(0o600)
    assert read_private_bytes(path) == b"proof"

    path.chmod(0o640)
    with pytest.raises(ValueError, match="mode-0600 regular file"):
        read_private_bytes(path)

    path.unlink()
    path.mkdir(mode=0o600)
    with pytest.raises(ValueError, match="mode-0600 regular file"):
        read_private_bytes(path)

    path.rmdir()
    target = tmp_path / "target"
    target.write_bytes(b"proof")
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(PrivateFileOpenError):
        read_private_bytes(path)


def test_atomic_replace_bytes_handles_short_writes_and_fsyncs_file_before_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receipt"
    path.write_bytes(b"old")
    path.chmod(0o600)
    writes: list[int] = []
    fsync_kinds: list[str] = []
    real_write, real_fsync = os.write, os.fsync

    def short_write(descriptor: int, data: bytes) -> int:
        writes.append(len(data))
        return real_write(descriptor, data[:1])

    def observe_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        fsync_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(secure_file.os, "write", short_write)
    monkeypatch.setattr(secure_file.os, "fsync", observe_fsync)
    atomic_replace_bytes(path, b"new proof")

    assert writes == list(range(len(b"new proof"), 0, -1))
    assert fsync_kinds == ["file", "directory"]
    assert path.read_bytes() == b"new proof"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_atomic_replace_bytes_preserves_target_and_removes_temporary_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receipt"
    path.write_bytes(b"old")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(secure_file.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        atomic_replace_bytes(path, b"new")

    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


def test_create_new_private_bytes_is_exclusive_and_handles_short_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receipt"
    calls = 0
    real_write = os.write

    def short_write(descriptor: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        return real_write(descriptor, data[:2])

    monkeypatch.setattr(secure_file.os, "write", short_write)
    create_new_private_bytes(path, b"proof")

    assert calls == 3
    assert path.read_bytes() == b"proof"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        create_new_private_bytes(path, b"replacement")
    assert path.read_bytes() == b"proof"
