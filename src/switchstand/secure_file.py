"""Small byte-level primitives for private durable files."""

from __future__ import annotations

import os
import stat
import tempfile
from hashlib import sha256
from pathlib import Path


class PrivateFileOpenError(OSError):
    """The private path could not be opened without following a final symlink."""


class PrivateFileDurabilityUnknown(OSError):
    """A complete create-new file is visible, but parent durability is unknown."""

    def __init__(
        self,
        path: Path,
        expected_digest: str,
        observed_digest: str | None,
        residue_path: Path | None = None,
    ) -> None:
        super().__init__(f"private file is visible but durability is unknown: {path}")
        self.path = path
        self.expected_digest = expected_digest
        self.observed_digest = observed_digest
        self.residue_path = residue_path


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written == 0:
            raise OSError("short private-file write")
        offset += written


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_private_bytes(path: Path) -> bytes:
    """Read an exact mode-0600 regular file without following a final symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise PrivateFileOpenError(str(path)) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o777 != 0o600:
            raise ValueError("not an exact mode-0600 regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def atomic_replace_bytes(path: Path, data: bytes) -> None:
    """Durably replace a private file through a same-directory mode-0600 temporary."""
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.replace(temporary, path)
        _fsync_parent(path)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def create_new_private_bytes(path: Path, data: bytes) -> None:
    """Durably create one mode-0600 file without replacing an existing path."""
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    published = False
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.link(temporary, path, follow_symlinks=False)
        published = True
        try:
            temporary.unlink()
            _fsync_parent(path)
        except OSError as error:
            expected = sha256(data).hexdigest()
            try:
                observed = sha256(read_private_bytes(path)).hexdigest()
            except OSError, ValueError:
                observed = None
            residue = temporary if temporary.exists() else None
            raise PrivateFileDurabilityUnknown(path, expected, observed, residue) from error
    finally:
        os.close(descriptor)
        if published:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            temporary.unlink(missing_ok=True)
