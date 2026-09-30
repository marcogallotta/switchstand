"""Offline, default-off FastMCP OAuth state migration and recovery receipts."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

WRITER_SERVICES = (
    "switchstand-chatgpt-mcp.service",
    "switchstand-stable-auth.service",
)
STATE_CATEGORIES = {
    "clients": "oauth_proxy_clients",
    "jti_mappings": "jti_mappings",
    "refresh_tokens": "refresh_tokens",
    "upstream_tokens": "upstream_tokens",
}
REQUIRED_STATE_CATEGORIES = frozenset({"clients", "jti_mappings", "upstream_tokens"})
CopyKind = Literal["backup", "migration", "restore"]


class MigrationFailure(RuntimeError):
    """A definite offline qualification failure with no claimed successful copy."""


class WriterProbe(Protocol):
    def inactive(self) -> Mapping[str, bool]: ...


class SystemdWriterProbe:
    """Read only the two exact single-host writer units."""

    def inactive(self) -> Mapping[str, bool]:
        result: dict[str, bool] = {}
        for service in WRITER_SERVICES:
            completed = subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    service,
                    "--property=ActiveState,MainPID",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            values = dict(
                line.split("=", 1)
                for line in completed.stdout.splitlines()
                if "=" in line
            )
            result[service] = (
                completed.returncode == 0
                and values.get("ActiveState") in {"inactive", "failed"}
                and values.get("MainPID") == "0"
            )
        return result


@dataclass(frozen=True, slots=True)
class StateManifest:
    tree_sha256: str
    logical_tree_sha256: str
    files: int
    bytes: int
    category_sha256: dict[str, str]
    logical_category_sha256: dict[str, str]


def _path_digest(records: list[tuple[str, int, int, str]]) -> str:
    encoded = json.dumps(records, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def state_manifest(root: Path) -> StateManifest:
    """Hash exact relative paths, modes, sizes, and bytes without exposing contents."""
    _owned_directory(root, "state root")
    records: list[tuple[str, int, int, str]] = []
    logical_records: list[tuple[str, int, int, str]] = []
    category_records: dict[str, list[tuple[str, int, int, str]]] = {
        name: [] for name in STATE_CATEGORIES
    }
    logical_category_records: dict[str, list[tuple[str, int, int, str]]] = {
        name: [] for name in STATE_CATEGORIES
    }
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if metadata.st_uid != os.getuid() or path.is_symlink():
            raise MigrationFailure("state tree contains an unowned or symbolic entry")
        if path.is_dir():
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise MigrationFailure("state tree contains a non-regular or multiply-linked file")
        digest = _file_digest(path)
        record = (relative, stat.S_IMODE(metadata.st_mode), metadata.st_size, digest)
        records.append(record)
        logical_digest, logical_size = _logical_file_digest(path, root)
        logical_record = (
            relative,
            stat.S_IMODE(metadata.st_mode),
            logical_size,
            logical_digest,
        )
        logical_records.append(logical_record)
        total_bytes += metadata.st_size
        normalized = relative.replace("-", "_")
        for category, marker in STATE_CATEGORIES.items():
            if marker in normalized:
                category_records[category].append(record)
                logical_category_records[category].append(logical_record)
    missing = [
        name
        for name, values in category_records.items()
        if name in REQUIRED_STATE_CATEGORIES and not values
    ]
    if missing:
        raise MigrationFailure("state tree lacks required encrypted store categories")
    return StateManifest(
        _path_digest(records),
        _path_digest(logical_records),
        len(records),
        total_bytes,
        {name: _path_digest(values) for name, values in category_records.items()},
        {
            name: _path_digest(values)
            for name, values in logical_category_records.items()
        },
    )


def _file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _filetree_info(path: Path, root: Path) -> tuple[dict[str, object], Path] | None:
    if not (path.name.startswith("S_mcp_") and path.name.endswith("-info.json")):
        return None
    try:
        raw_payload: object = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise MigrationFailure("FastMCP FileTree metadata is malformed") from exc
    if not isinstance(raw_payload, dict):
        raise MigrationFailure("FastMCP FileTree metadata has an unsupported schema")
    payload = cast(dict[str, object], raw_payload)
    if (
        set(payload) != {"collection", "created_at", "directory", "version"}
        or not isinstance(payload["collection"], str)
        or not payload["collection"]
        or not isinstance(payload["created_at"], str)
        or not payload["created_at"]
        or not isinstance(payload["directory"], str)
        or type(payload["version"]) is not int
        or payload["version"] != 1
    ):
        raise MigrationFailure("FastMCP FileTree metadata has an unsupported schema")
    expected = path.with_name(path.name.removesuffix("-info.json"))
    try:
        expected.relative_to(root)
    except ValueError as exc:
        raise MigrationFailure("FastMCP FileTree directory escapes the state root") from exc
    if payload["directory"] != str(expected):
        raise MigrationFailure("FastMCP FileTree directory does not match its state root")
    _owned_directory(expected, "FastMCP FileTree collection directory")
    return payload, expected


def _logical_file_digest(path: Path, root: Path) -> tuple[str, int]:
    info = _filetree_info(path, root)
    if info is None:
        return _file_digest(path), path.stat().st_size
    payload, expected = info
    payload["directory"] = f"$STATE_ROOT/{expected.relative_to(root).as_posix()}"
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def _relocate_filetree_info(source: Path, target: Path) -> None:
    for path in sorted(target.rglob("S_mcp_*-info.json"), key=lambda item: item.as_posix()):
        relative = path.relative_to(target)
        source_path = source / relative
        source_info = _filetree_info(source_path, source)
        if source_info is None:  # pragma: no cover - selected by the filename glob
            raise MigrationFailure("expected FastMCP FileTree metadata")
        payload, _source_directory = source_info
        target_directory = path.with_name(path.name.removesuffix("-info.json"))
        payload["directory"] = str(target_directory)
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                offset = 0
                while offset < len(encoded):
                    written = os.write(descriptor, encoded[offset:])
                    if written == 0:
                        raise OSError("short metadata write")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise MigrationFailure(
                "FastMCP FileTree relocation failed; preserve the partial target as evidence"
            ) from exc


def _owned_directory(path: Path, label: str) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise MigrationFailure(f"{label} must be an absolute owned real directory")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise MigrationFailure(f"{label} is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise MigrationFailure(f"{label} must be an absolute owned real directory")


def _assert_distinct(source: Path, target: Path) -> None:
    try:
        source_parts = source.resolve(strict=True).parts
        target_parts = (target.parent.resolve(strict=True) / target.name).parts
    except OSError as exc:
        raise MigrationFailure("copy source or target parent is unavailable") from exc
    if source_parts == target_parts or source_parts == target_parts[: len(source_parts)]:
        raise MigrationFailure("copy target must not be inside the source state")
    if target_parts == source_parts[: len(target_parts)]:
        raise MigrationFailure("copy source must not be inside the target")


def _resolved_candidate(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise MigrationFailure(f"{label} must be absolute and symlink-free")
    try:
        return path.resolve(strict=False)
    except OSError as exc:
        raise MigrationFailure(f"{label} cannot be resolved") from exc


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _preflight_control_paths(
    *, roots: tuple[Path, ...], receipt_path: Path, lock_path: Path
) -> None:
    resolved_roots = tuple(_resolved_candidate(root, "state path") for root in roots)
    receipt = _resolved_candidate(receipt_path, "receipt path")
    lock = _resolved_candidate(lock_path, "migration lock path")
    if _paths_overlap(receipt, lock) or any(
        _paths_overlap(control, root)
        for control in (receipt, lock)
        for root in resolved_roots
    ):
        raise MigrationFailure("state and control paths must not overlap")


def _receipt_preflight(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise MigrationFailure("receipt path must be absolute and symlink-free")
    _owned_directory(path.parent, "receipt parent")
    if path.exists():
        raise MigrationFailure("receipt target must not already exist")


def _writers_stopped(probe: WriterProbe) -> dict[str, bool]:
    observed = dict(probe.inactive())
    if set(observed) != set(WRITER_SERVICES) or not all(observed.values()):
        raise MigrationFailure("exclusive-writer stop is not proven")
    return observed


@contextmanager
def _migration_lock(path: Path) -> Generator[None]:
    if not path.is_absolute() or path.is_symlink():
        raise MigrationFailure("migration lock path must be absolute and symlink-free")
    _owned_directory(path.parent, "migration lock parent")
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise MigrationFailure("migration lock is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise MigrationFailure("migration lock must be an owned mode-0600 regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationFailure("another migration transaction holds the lock") from exc
        yield
    finally:
        os.close(descriptor)


def _copy_state(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise MigrationFailure("copy target must not exist")
    _owned_directory(target.parent, "copy target parent")
    try:
        shutil.copytree(source, target, symlinks=True, copy_function=shutil.copy2)
        for path in sorted(target.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_file() and not path.is_symlink():
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
            elif path.is_dir() and not path.is_symlink():
                descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        descriptor = os.open(target.parent, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise MigrationFailure("state copy failed; preserve the partial target as evidence") from exc


def _key_fingerprint(secret: str) -> str:
    if len(secret) < 32:
        raise MigrationFailure("signing material is missing or implausibly short")
    return hashlib.sha256(secret.encode()).hexdigest()


def _write_receipt(path: Path, receipt: dict[str, object]) -> None:
    _receipt_preflight(path)
    encoded = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    temporary: Path | None = None
    published = False
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written == 0:
                    raise OSError("short receipt write")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.link(temporary, path, follow_symlinks=False)
        published = True
        temporary.unlink()
        temporary = None
        parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as exc:
        if published:
            try:
                path.unlink()
            except OSError:
                pass
        raise MigrationFailure("receipt publication failed") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def copy_with_receipt(
    *,
    kind: CopyKind,
    source: Path,
    target: Path,
    receipt_path: Path,
    lock_path: Path,
    signing_material: str,
    probe: WriterProbe,
    clock: Callable[[], float] = time.time,
) -> dict[str, object]:
    """Copy an offline state tree to an absent target and bind an immutable receipt."""
    if kind not in {"backup", "migration", "restore"}:
        raise MigrationFailure("unsupported state-copy kind")
    _preflight_control_paths(
        roots=(source, target), receipt_path=receipt_path, lock_path=lock_path
    )
    _assert_distinct(source, target)
    with _migration_lock(lock_path):
        _receipt_preflight(receipt_path)
        signing_material_sha256 = _key_fingerprint(signing_material)
        writer_state = _writers_stopped(probe)
        before = state_manifest(source)
        _copy_state(source, target)
        _relocate_filetree_info(source, target)
        after = state_manifest(source)
        copied = state_manifest(target)
        writer_state_after = _writers_stopped(probe)
        if before != after or (
            before.logical_tree_sha256 != copied.logical_tree_sha256
            or before.logical_category_sha256 != copied.logical_category_sha256
        ):
            raise MigrationFailure("source or copied state changed during the offline transaction")
        receipt: dict[str, object] = {
            "schema": "switchstand.stable-auth-state-copy.v1",
            "kind": kind,
            "created_at": int(clock()),
            "source": str(source),
            "target": str(target),
            "manifest": asdict(copied),
            "source_manifest": asdict(after),
            "signing_material_sha256": signing_material_sha256,
            "writers_before": writer_state,
            "writers_after": writer_state_after,
            "automatic_oauth_restore": False,
        }
        _write_receipt(receipt_path, receipt)
        return receipt


def rollback_receipt(
    *,
    state: Path,
    expected_tree_sha256: str,
    receipt_path: Path,
    lock_path: Path,
    signing_material: str,
    probe: WriterProbe,
    clock: Callable[[], float] = time.time,
) -> dict[str, object]:
    """Prove rollback preserves current OAuth state; never restore a snapshot."""
    _preflight_control_paths(
        roots=(state,), receipt_path=receipt_path, lock_path=lock_path
    )
    with _migration_lock(lock_path):
        _receipt_preflight(receipt_path)
        signing_material_sha256 = _key_fingerprint(signing_material)
        writer_state_before = _writers_stopped(probe)
        before = state_manifest(state)
        if before.tree_sha256 != expected_tree_sha256:
            raise MigrationFailure("current OAuth state does not match the rollback expectation")
        after = state_manifest(state)
        writer_state_after = _writers_stopped(probe)
        if before != after:
            raise MigrationFailure("current OAuth state changed during rollback verification")
        receipt: dict[str, object] = {
            "schema": "switchstand.stable-auth-rollback.v1",
            "created_at": int(clock()),
            "state": str(state),
            "manifest": asdict(after),
            "signing_material_sha256": signing_material_sha256,
            "writers_before": writer_state_before,
            "writers_after": writer_state_after,
            "action": "preserve-current-oauth-state",
            "automatic_oauth_restore": False,
        }
        _write_receipt(receipt_path, receipt)
        return receipt


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    copy = subcommands.add_parser("copy")
    copy.add_argument("--kind", choices=("backup", "migration", "restore"), required=True)
    copy.add_argument("--source", type=Path, required=True)
    copy.add_argument("--target", type=Path, required=True)
    copy.add_argument("--receipt", type=Path, required=True)
    copy.add_argument("--lock-file", type=Path, required=True)
    rollback = subcommands.add_parser("rollback-receipt")
    rollback.add_argument("--state", type=Path, required=True)
    rollback.add_argument("--expected-tree-sha256", required=True)
    rollback.add_argument("--receipt", type=Path, required=True)
    rollback.add_argument("--lock-file", type=Path, required=True)
    arguments = parser.parse_args()
    signing_material = os.getenv("SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET", "")
    if arguments.command == "copy":
        result = copy_with_receipt(
            kind=arguments.kind,
            source=arguments.source,
            target=arguments.target,
            receipt_path=arguments.receipt,
            lock_path=arguments.lock_file,
            signing_material=signing_material,
            probe=SystemdWriterProbe(),
        )
    else:
        result = rollback_receipt(
            state=arguments.state,
            expected_tree_sha256=arguments.expected_tree_sha256,
            receipt_path=arguments.receipt,
            lock_path=arguments.lock_file,
            signing_material=signing_material,
            probe=SystemdWriterProbe(),
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    run()
