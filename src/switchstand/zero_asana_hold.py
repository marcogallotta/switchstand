"""Fail-closed production hold for an integrated zero-Asana cutover."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .edge_maintenance import (
    Config as EdgeConfig,
)
from .edge_maintenance import (
    Failed,
    HostOperations,
    Unknown,
    exclusive_lock,
    run_host_command,
    validate_target,
)
from .work_corpus import (
    CorpusProvider,
    capture_manifest_connection,
    compare_manifests,
    write_manifest,
)
from .work_source_export import SourceProvider, source_parity_connection

HOST_MACHINE = "marco@.host"
TombstoneBuilder = Callable[[dict[str, object], Path], Awaitable[Path | None]]

class HoldProvider(CorpusProvider, SourceProvider, Protocol): ...

@dataclass(frozen=True)
class HoldPaths:
    attempt_dir: Path
    corpus_a: Path
    source_export: Path
    corpus_b: Path
    tombstones: Path
    fastmcp_snapshot: Path
    prepared_receipt: Path
    final_receipt: Path

    @classmethod
    def create(cls, attempt_dir: Path) -> HoldPaths:
        return cls(
            attempt_dir=attempt_dir,
            corpus_a=attempt_dir / "corpus-a.json",
            source_export=attempt_dir / "source-parity.json",
            corpus_b=attempt_dir / "corpus-b.json",
            tombstones=attempt_dir / "tombstones.json",
            fastmcp_snapshot=attempt_dir / "fastmcp.after-stop.tar",
            prepared_receipt=attempt_dir / "prepared-receipt.json",
            final_receipt=attempt_dir / "production-hold-receipt.json",
        )

@dataclass(frozen=True)
class HoldArtifacts:
    paths: HoldPaths
    source_candidate: str
    corpus_sha256: str
    source_parity_sha256: str
    corpus_file_sha256: str
    source_parity_file_sha256: str
    tombstones_sha256: str | None
    fastmcp_snapshot_sha256: str
    database_identity_sha256: str

class HoldOperations(Protocol):
    @property
    def lock_path(self) -> Path: ...
    def preflight(self, paths: HoldPaths, source_candidate: str) -> None: ...
    def install_gate(self) -> None: ...
    def prove_gate(self) -> None: ...
    def stop_and_prove(self) -> None: ...
    def snapshot_current(self, target: Path) -> str: ...

class HostHoldOperations:
    """Production host operations routed through the exact host systemd machine."""

    def __init__(self, edge_config: EdgeConfig):
        self.config = edge_config
        self.edge = HostOperations(edge_config)
    @property
    def lock_path(self) -> Path:
        return self.config.lock_path
    def _systemctl(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return run_host_command(
            ["systemctl", "--user", f"--machine={HOST_MACHINE}", *arguments],
            check=check,
        )
    def preflight(self, paths: HoldPaths, source_candidate: str) -> None:
        validate_target(self.config)
        exact_paths = HoldPaths.create(self.config.attempt_dir)
        if paths != exact_paths or source_candidate != self.config.candidate_sha:
            raise Failed("hold attempt or source candidate does not match edge configuration")
        metadata = paths.attempt_dir.stat()
        if (
            not paths.attempt_dir.is_absolute()
            or paths.attempt_dir.is_symlink()
            or not paths.attempt_dir.is_dir()
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o777 != 0o700
        ):
            raise Failed("hold attempt directory is not private and exact")
        outputs = tuple(value for key, value in asdict(paths).items() if key != "attempt_dir")
        if self.config.fastmcp_state.is_symlink() or not self.config.fastmcp_state.is_dir():
            raise Failed("FastMCP state is not an exact directory")
        if any(os.path.lexists(path) for path in outputs):
            raise Failed("hold output already exists")
        unit = self._systemctl(
            "show", self.config.service, "--property=ExecStart", "--value"
        ).stdout
        if str(self.config.launcher) not in unit:
            raise Failed("host edge unit does not execute the selected launcher")
        if self._systemctl("is-active", self.config.service, check=False).stdout.strip() != "active":
            raise Failed("host edge service is not active")
        if self.edge.gate_exact():
            raise Failed("maintenance gate already exists")
    def install_gate(self) -> None:
        self.edge.gate()
    def prove_gate(self) -> None:
        if not self.edge.gate_exact() or not self.edge.public_gated():
            raise Unknown("maintenance gate is not exact at public ingress")
    def stop_and_prove(self) -> None:
        self._systemctl("stop", self.config.service, check=False)
        state = self._systemctl("is-active", self.config.service, check=False).stdout.strip()
        if state != "inactive":
            raise Unknown("host edge service did not become inactive")
        endpoint = urlparse(self.config.local_url)
        if endpoint.hostname is None or endpoint.port is None:
            raise Unknown("edge listener identity is invalid")
        try:
            with socket.create_connection((endpoint.hostname, endpoint.port), timeout=1):
                pass
        except OSError:
            return
        raise Unknown("edge listener remains after host service stop")
    def snapshot_current(self, target: Path) -> str:
        temporary = target.with_suffix(".host-tmp")
        if target.exists() or temporary.exists():
            raise Failed("FastMCP snapshot output already exists")
        try:
            run_host_command([
                "systemd-run",
                "--user",
                f"--machine={HOST_MACHINE}",
                "--wait",
                "--collect",
                "--quiet",
                "--",
                "/usr/bin/tar",
                "-C",
                str(self.config.fastmcp_state.parent),
                "-cf",
                str(temporary),
                self.config.fastmcp_state.name,
            ])
            os.chmod(temporary, 0o600)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.link(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return _file_digest(target)
        except (OSError, subprocess.SubprocessError) as error:
            raise Failed("current FastMCP snapshot failed") from error
        finally:
            temporary.unlink(missing_ok=True)

def _file_digest(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise Failed(f"hold artifact is not a regular owned file: {path.name}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    finally:
        os.close(descriptor)

async def _database_identity(connection: AsyncConnection) -> str:
    rows = (await connection.execute(text("""
        SELECT 'binding', provider, provider_work_id, id::text, '', '' FROM work_handles
        UNION ALL
        SELECT 'event', provider, provider_work_id, id::text, provider_event_id, work_id::text
        FROM work_event_handles ORDER BY 1, 2, 3, 4, 5, 6
    """))).all()
    encoded = json.dumps([[str(value) for value in row] for row in rows], separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

def _digested(document: dict[str, object]) -> dict[str, object]:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return document | {"sha256": hashlib.sha256(encoded).hexdigest()}

def _prepared_receipt(source_candidate: str, paths: HoldPaths) -> dict[str, object]:
    return _digested({
        "schema_version": 1,
        "phase": "PREPARED",
        "terminal": False,
        "source_candidate": source_candidate,
        "host_machine": HOST_MACHINE,
        "attempt_dir": str(paths.attempt_dir),
        "direct_asana_writes": "COORDINATION_LIMITATION_NOT_TECHNICALLY_FROZEN",
    })

def _final_receipt(artifacts: HoldArtifacts) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "phase": "PRODUCTION_HOLD",
        "terminal": True,
        "source_candidate": artifacts.source_candidate,
        "host_machine": HOST_MACHINE,
        "fastmcp_snapshot": str(artifacts.paths.fastmcp_snapshot),
        "fastmcp_snapshot_sha256": artifacts.fastmcp_snapshot_sha256,
        "corpus_a": str(artifacts.paths.corpus_a),
        "corpus_b": str(artifacts.paths.corpus_b),
        "corpus_sha256": artifacts.corpus_sha256,
        "corpus_file_sha256": artifacts.corpus_file_sha256,
        "source_parity": str(artifacts.paths.source_export),
        "source_parity_sha256": artifacts.source_parity_sha256,
        "source_parity_file_sha256": artifacts.source_parity_file_sha256,
        "tombstones": (
            str(artifacts.paths.tombstones) if artifacts.tombstones_sha256 else None
        ),
        "tombstones_sha256": artifacts.tombstones_sha256,
        "database_identity_sha256": artifacts.database_identity_sha256,
        "postgres_transaction": "SHARE_LOCKS_HELD_CONTINUOUSLY_THROUGH_COMMIT_THEN_RELEASED",
        "maintenance_flock": "HELD_THROUGH_FINAL_RECEIPT",
        "edge_gate": "PROVEN_EXACT",
        "edge_service": "PROVEN_STOPPED",
        "direct_asana_writes": "COORDINATION_LIMITATION_NOT_TECHNICALLY_FROZEN",
    }
    return _digested(document)

async def production_hold[Result](
    engine: AsyncEngine,
    provider: HoldProvider,
    source_candidate: str,
    paths: HoldPaths,
    operations: HoldOperations,
    build_tombstones: TombstoneBuilder,
    continuation: Callable[[AsyncConnection, HoldArtifacts], Awaitable[Result]],
) -> Result:
    """Hold ingress, service, DB locks, and flock through one integrated continuation."""
    if len(source_candidate) != 40 or any(
        character not in "0123456789abcdef" for character in source_candidate
    ):
        raise ValueError("source candidate must be a lowercase 40-character Git SHA")
    with exclusive_lock(operations.lock_path):
        operations.preflight(paths, source_candidate)
        write_manifest(paths.prepared_receipt, _prepared_receipt(source_candidate, paths))
        operations.install_gate()
        operations.prove_gate()
        operations.stop_and_prove()
        snapshot_digest = operations.snapshot_current(paths.fastmcp_snapshot)
        async with engine.connect() as connection, connection.begin():
            await connection.execute(text(
                "LOCK TABLE work_handles, work_event_handles IN SHARE MODE"
            ))
            identity_a = await _database_identity(connection)
            corpus_a = await capture_manifest_connection(
                connection, provider, source_candidate
            )
            write_manifest(paths.corpus_a, corpus_a)
            tombstone_path = await build_tombstones(corpus_a, paths.tombstones)
            if tombstone_path not in (None, paths.tombstones) or (
                (tombstone_path is None) != (not paths.tombstones.exists())
            ):
                raise Failed("tombstone builder did not bind the exact hold output")
            source = await source_parity_connection(
                connection,
                provider,
                paths.corpus_a,
                tombstone_path,
            )
            write_manifest(paths.source_export, source)
            corpus_b = await capture_manifest_connection(
                connection, provider, source_candidate
            )
            write_manifest(paths.corpus_b, corpus_b)
            corpus_digest = compare_manifests(paths.corpus_a, paths.corpus_b)
            identity_b = await _database_identity(connection)
            if identity_a != identity_b:
                raise Unknown("database binding/event identity changed during source export")
            artifacts = HoldArtifacts(
                paths=paths,
                source_candidate=source_candidate,
                corpus_sha256=corpus_digest,
                source_parity_sha256=str(source["sha256"]),
                corpus_file_sha256=_file_digest(paths.corpus_a),
                source_parity_file_sha256=_file_digest(paths.source_export),
                tombstones_sha256=(
                    _file_digest(tombstone_path) if tombstone_path is not None else None
                ),
                fastmcp_snapshot_sha256=snapshot_digest,
                database_identity_sha256=identity_a,
            )
            result = await continuation(connection, artifacts)
        write_manifest(paths.final_receipt, _final_receipt(artifacts))
        return result
