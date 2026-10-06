import os
import stat
from pathlib import Path

import pytest

from switchstand import secure_file
from switchstand.secure_file import (
    PrivateFileDurabilityUnknown,
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


def test_read_private_bytes_enforces_bound_without_partial_result(tmp_path: Path) -> None:
    path = tmp_path / "receipt"
    path.write_bytes(b"proof")
    path.chmod(0o600)

    assert read_private_bytes(path, max_bytes=5) == b"proof"
    with pytest.raises(ValueError, match="exceeds maximum size"):
        read_private_bytes(path, max_bytes=4)
    with pytest.raises(ValueError, match="nonnegative"):
        read_private_bytes(path, max_bytes=-1)


def test_read_private_bytes_rechecks_bound_after_stale_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receipt"
    path.write_bytes(b"proof")
    path.chmod(0o600)
    real_fstat = os.fstat

    def stale_size(descriptor: int) -> os.stat_result:
        metadata = list(real_fstat(descriptor))
        metadata[6] = 4
        return os.stat_result(metadata)

    monkeypatch.setattr(secure_file.os, "fstat", stale_size)
    with pytest.raises(ValueError, match="exceeds maximum size"):
        read_private_bytes(path, max_bytes=4)


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


def test_create_new_private_bytes_is_exclusive_durable_and_handles_short_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receipt"
    calls = 0
    fsync_kinds: list[str] = []
    real_write = os.write
    real_fsync = os.fsync

    def short_write(descriptor: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        return real_write(descriptor, data[:2])

    def observe_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        fsync_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(secure_file.os, "write", short_write)
    monkeypatch.setattr(secure_file.os, "fsync", observe_fsync)
    create_new_private_bytes(path, b"proof")

    assert calls == 3
    assert fsync_kinds == ["file", "directory"]
    assert path.read_bytes() == b"proof"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        create_new_private_bytes(path, b"replacement")
    assert path.read_bytes() == b"proof"


@pytest.mark.parametrize("failure", ["write", "file_fsync"])
def test_create_new_private_bytes_does_not_publish_incomplete_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "receipt"
    real_write, real_fsync = os.write, os.fsync

    def fail_write(descriptor: int, data: bytes) -> int:
        real_write(descriptor, data[:1])
        raise OSError("write failed")

    def fail_file_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("file fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(secure_file.os, "write", fail_write if failure == "write" else real_write)
    monkeypatch.setattr(
        secure_file.os, "fsync", fail_file_fsync if failure == "file_fsync" else real_fsync
    )

    with pytest.raises(OSError, match=failure.replace("_", " ")):
        create_new_private_bytes(path, b"proof")

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_create_new_private_bytes_reports_exact_visible_content_when_parent_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receipt"

    def fail_parent(_path: Path) -> None:
        raise OSError("parent fsync failed")

    monkeypatch.setattr(secure_file, "_fsync_parent", fail_parent)
    with pytest.raises(PrivateFileDurabilityUnknown) as caught:
        create_new_private_bytes(path, b"proof")

    assert caught.value.path == path
    assert caught.value.expected_digest == caught.value.observed_digest
    assert caught.value.residue_path is None
    assert path.read_bytes() == b"proof"
    assert list(tmp_path.iterdir()) == [path]
    with pytest.raises(FileExistsError):
        create_new_private_bytes(path, b"replacement")


def test_create_new_private_bytes_preserves_typed_ambiguity_on_persistent_unlink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receipt"
    real_unlink = Path.unlink

    def fail_temporary_unlink(candidate: Path, *, missing_ok: bool = False) -> None:
        if candidate.name.startswith(f".{path.name}."):
            raise OSError("temporary unlink failed")
        real_unlink(candidate, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)
    with pytest.raises(PrivateFileDurabilityUnknown) as caught:
        create_new_private_bytes(path, b"proof")

    error = caught.value
    assert error.expected_digest == error.observed_digest
    assert path.read_bytes() == b"proof"
    assert error.residue_path is not None
    assert error.residue_path.exists()
    assert error.residue_path.read_bytes() == b"proof"
    assert stat.S_IMODE(error.residue_path.stat().st_mode) == 0o600


def test_create_new_private_bytes_loses_atomic_race_without_replacing_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receipt"
    real_link = os.link

    def publish_winner(source: Path, target: Path, *, follow_symlinks: bool) -> None:
        path.write_bytes(b"winner")
        real_link(source, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(secure_file.os, "link", publish_winner)
    with pytest.raises(FileExistsError):
        create_new_private_bytes(path, b"loser")

    assert path.read_bytes() == b"winner"
    assert list(tmp_path.iterdir()) == [path]
