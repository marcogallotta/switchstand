"""Provision and remove one copied-state, disposable migration target."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from .development import development_subnet
from .edge_maintenance import Failed, Unknown, exclusive_lock
from .stage12_cutover import read_private

REHEARSALS = Path("/home/marco/.local/state/switchstand/rehearsals")
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
FAILURE_DETAIL_LIMIT = 2048


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=True, timeout=300)


def _private_directory(path: Path, *, create: bool = False) -> None:
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or path.is_symlink()
            or path.resolve(strict=True) != path
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise OSError
    except OSError as error:
        raise Failed(f"private rehearsal directory is not exact: {path}") from error


def _source(path: Path, label: str) -> tuple[Path, bytes, str]:
    path = path.absolute()
    data = read_private(path, label)
    return path, data, hashlib.sha256(data).hexdigest()


def _ports() -> list[int]:
    sockets: list[socket.socket] = []
    try:
        for _ in range(3):
            item = socket.socket()
            item.bind(("127.0.0.1", 0))
            sockets.append(item)
        return [cast(tuple[str, int], item.getsockname())[1] for item in sockets]
    finally:
        for item in sockets:
            item.close()


def _write(path: Path, value: dict[str, Any], *, replace: bool = False) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".target.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if not replace and path.exists():
            raise Failed("disposable target descriptor already exists")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _write_bytes(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _identity(name: str) -> dict[str, str]:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", name) or name == "production":
        raise Failed("disposable target name is invalid")
    project = f"switchstand-rehearsal-{name}"
    return {
        "name": name,
        "project": project,
        "compose_service": "postgres",
        "edge_service": f"{project}.service",
        "volume": f"{project}_postgres-data",
        "network": f"{project}_default",
        "subnet": development_subnet(REHEARSALS, name),
    }


def _extract(snapshot: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    try:
        with tarfile.open(snapshot) as archive:
            members = archive.getmembers()
            if not members or any(
                member.name.split("/", 1)[0] != "fastmcp" or member.issym() or member.islnk()
                for member in members
            ):
                raise Failed("FastMCP snapshot is not a bounded regular tree")
            archive.extractall(destination, filter="data")
        copied = destination / "fastmcp"
        info = copied.lstat()
        if not stat.S_ISDIR(info.st_mode) or copied.is_symlink() or info.st_uid != os.getuid():
            raise Failed("copied FastMCP state is not an exact private directory")
        copied.chmod(0o700)
    except (OSError, tarfile.TarError) as error:
        raise Failed("FastMCP snapshot cannot be copied") from error


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    try:
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            relative = path.relative_to(root).as_posix().encode()
            if path.is_symlink() or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise OSError
            digest.update(relative + (b"/\0" if stat.S_ISDIR(info.st_mode) else b"\0"))
            if stat.S_ISREG(info.st_mode):
                descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
                try:
                    with os.fdopen(descriptor, "rb", closefd=False) as stream:
                        digest.update(hashlib.file_digest(stream, "sha256").digest())
                finally:
                    os.close(descriptor)
    except OSError as error:
        raise Failed("copied FastMCP state is not an exact regular tree") from error
    return digest.hexdigest()


def _docker_identity(value: dict[str, Any], expected: dict[str, str]) -> None:
    config, network_settings, mounts = (
        value.get("Config"), value.get("NetworkSettings"), value.get("Mounts")
    )
    labels = cast(dict[str, object], config).get("Labels") if isinstance(config, dict) else None
    networks = (
        cast(dict[str, object], network_settings).get("Networks")
        if isinstance(network_settings, dict) else None
    )
    if not isinstance(labels, dict) or not isinstance(networks, dict) or not isinstance(mounts, list):
        raise Failed("provisioned PostgreSQL identity is not exact")
    exact_labels = cast(dict[str, object], labels)
    exact_networks = cast(dict[str, object], networks)
    exact_mounts = cast(list[object], mounts)
    if (
        exact_labels.get("com.docker.compose.project") != expected["project"]
        or exact_labels.get("com.docker.compose.service") != "postgres"
        or set(exact_networks) != {expected["network"]}
        or len(exact_mounts) != 1
        or not isinstance(exact_mounts[0], dict)
        or any(
            cast(dict[str, object], exact_mounts[0]).get(key) != expected_value
            for key, expected_value in {
                "Type": "volume", "Name": expected["volume"],
                "Destination": "/var/lib/postgresql",
            }.items()
        )
    ):
        raise Failed("provisioned PostgreSQL identity is not exact")


def _failure(step: str, error: BaseException) -> dict[str, object]:
    result: dict[str, object] = {"step": step, "kind": type(error).__name__}
    if isinstance(error, subprocess.CalledProcessError):
        result["exit_code"] = error.returncode
        detail = (error.stderr or "").strip()
        result["stderr"] = "".join(
            character if character in "\n\t" or character.isprintable() else "?"
            for character in detail
        )[:FAILURE_DETAIL_LIMIT]
    return result


def provision(
    name: str, candidate_sha: str, runtime: Path, backup: Path, snapshot: Path, *, run: Runner = _run
) -> Path:
    """Create one private target and populate it from exact source artifacts."""
    identity = _identity(name)
    if not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
        raise Failed("candidate SHA is invalid")
    runtime = runtime.resolve(strict=True)
    try:
        observed = run(["git", "-C", str(runtime), "rev-parse", "HEAD"]).stdout.strip()
        dirty = run(["git", "-C", str(runtime), "status", "--porcelain"]).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise Failed("candidate runtime identity is unavailable") from error
    if observed != candidate_sha or dirty or not (runtime / "compose.state.yaml").is_file():
        raise Failed("candidate runtime does not match the exact candidate")
    backup, backup_data, backup_digest = _source(backup, "source database backup")
    snapshot, snapshot_data, snapshot_digest = _source(snapshot, "source FastMCP snapshot")
    _private_directory(REHEARSALS, create=True)
    root = REHEARSALS / name
    _private_directory(root, create=True)
    ports = _ports()
    sources = root / "sources"
    frozen_backup, frozen_snapshot = sources / "database.dump", sources / "fastmcp.tar"
    descriptor: dict[str, Any] = {
        "schema_version": 1,
        "status": "PROVISIONING",
        "candidate_sha": candidate_sha,
        "runtime": str(runtime),
        "root": str(root),
        "descriptor": str(root / "target.json"),
        "fastmcp_state": str(root / "copied-state" / "fastmcp"),
        "database_backup": str(backup),
        "database_backup_sha256": backup_digest,
        "fastmcp_snapshot": str(snapshot),
        "fastmcp_snapshot_sha256": snapshot_digest,
        "copied_database_backup": str(frozen_backup),
        "copied_fastmcp_snapshot": str(frozen_snapshot),
        "endpoints": {
            "caddy": f"http://127.0.0.1:{ports[0]}",
            "local": f"http://127.0.0.1:{ports[1]}/mcp",
            "public": f"http://127.0.0.1:{ports[2]}",
        },
        **identity,
    }
    path = root / "target.json"
    _write(path, descriptor)
    sources.mkdir(mode=0o700)
    _write_bytes(frozen_backup, backup_data)
    _write_bytes(frozen_snapshot, snapshot_data)
    _extract(frozen_snapshot, root / "copied-state")
    descriptor["fastmcp_tree_sha256"] = _tree_digest(root / "copied-state" / "fastmcp")
    compose = [
        "docker", "compose", "-p", identity["project"], "--project-directory", str(runtime),
        "-f", str(runtime / "compose.state.yaml"),
    ]
    step = "network_create"
    try:
        run([
            "docker", "network", "create", "--subnet", identity["subnet"],
            "--label", f"com.docker.compose.project={identity['project']}",
            "--label", "com.docker.compose.network=default", identity["network"],
        ])
        step = "network_verify"
        if not _owned_resource(
            "network", identity["network"], identity["project"],
            subnet=identity["subnet"], run=run,
        ):
            raise Failed("provisioned network identity is unavailable")
        step = "compose_up"
        run([*compose, "up", "-d", "--wait", "postgres"])
        step = "compose_container"
        container = run([*compose, "ps", "-q", "postgres"]).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", container):
            raise Failed("provisioned PostgreSQL identity is unavailable")
        descriptor["container"] = container
        _write(path, descriptor, replace=True)
        step = "container_inspect"
        inspected = json.loads(run(["docker", "inspect", container]).stdout)
        if len(inspected) != 1:
            raise Failed("provisioned PostgreSQL identity is unavailable")
        _docker_identity(inspected[0], identity)
        step = "database_copy"
        run(["docker", "cp", str(frozen_backup), f"{container}:/tmp/source.dump"])
        step = "database_restore"
        run([
            "docker", "exec", container, "pg_restore", "--exit-on-error", "--no-owner",
            "--no-privileges", "-U", "switchstand", "-d", "switchstand", "/tmp/source.dump",
        ])
        step = "database_copy_cleanup"
        run(["docker", "exec", container, "rm", "-f", "/tmp/source.dump"])
    except (Failed, IndexError, json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
        descriptor["status"] = "FAILED"
        descriptor["failure"] = _failure(step, error)
        _write(path, descriptor, replace=True)
        raise Failed(
            f"disposable PostgreSQL provisioning failed at {step}; use its descriptor to teardown"
        ) from error
    descriptor["status"] = "READY"
    descriptor["container"] = container
    _write(path, descriptor, replace=True)
    return path


def _load(name: str, *, failed: bool = False) -> tuple[Path, dict[str, Any]]:
    identity = _identity(name)
    _private_directory(REHEARSALS)
    root = REHEARSALS / name
    _private_directory(root)
    path = root / "target.json"
    try:
        raw: object = json.loads(read_private(path, "disposable target descriptor"))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise Failed("disposable target descriptor is invalid") from error
    if not isinstance(raw, dict):
        raise Failed("disposable target descriptor is invalid")
    value = cast(dict[str, Any], raw)
    if any(value.get(key) != item for key, item in identity.items()):
        raise Failed("disposable target descriptor owns a foreign identity")
    if value.get("root") != str(root) or value.get("descriptor") != str(path):
        raise Failed("disposable target descriptor escapes its root")
    status = value.get("status")
    failure = value.get("failure")
    valid_failure = (
        isinstance(failure, dict)
        and isinstance(cast(dict[str, object], failure).get("step"), str)
        and isinstance(cast(dict[str, object], failure).get("kind"), str)
    )
    if (
        status == "READY" and isinstance(value.get("container"), str)
    ):
        return root, value
    if failed and status == "FAILED" and valid_failure and (
        "container" not in value or isinstance(value.get("container"), str)
    ):
        return root, value
    raise Failed("disposable target descriptor is not exactly READY or FAILED")


def load_ready(
    name: str, candidate_sha: str, runtime: Path, *, run: Runner = _run,
) -> tuple[Path, dict[str, Any]]:
    """Read and prove the exact READY copied-state target without mutating it."""
    root, value = _load(name)
    expected = {
        "candidate_sha": candidate_sha,
        "runtime": str(runtime.resolve(strict=True)),
        "fastmcp_state": str(root / "copied-state" / "fastmcp"),
        "copied_database_backup": str(root / "sources" / "database.dump"),
        "copied_fastmcp_snapshot": str(root / "sources" / "fastmcp.tar"),
    }
    endpoints = value.get("endpoints")
    if any(value.get(key) != item for key, item in expected.items()) or not isinstance(
        endpoints, dict
    ):
        raise Failed("disposable target does not bind the exact candidate or copied state")
    exact_endpoints = cast(dict[str, object], endpoints)
    if set(exact_endpoints) != {"caddy", "local", "public"} or len(set(exact_endpoints.values())) != 3:
        raise Failed("disposable target endpoints are not exact")
    for path_key, digest_key in (
        ("copied_database_backup", "database_backup_sha256"),
        ("copied_fastmcp_snapshot", "fastmcp_snapshot_sha256"),
    ):
        data = read_private(Path(cast(str, value[path_key])), path_key)
        if hashlib.sha256(data).hexdigest() != value.get(digest_key):
            raise Failed("disposable copied-state digest changed")
    if _tree_digest(Path(cast(str, value["fastmcp_state"]))) != value.get("fastmcp_tree_sha256"):
        raise Failed("copied FastMCP state digest changed")
    try:
        ids = run([
            "docker", "ps", "-aq", "--no-trunc", "--filter",
            f"label=com.docker.compose.project={value['project']}",
        ]).stdout.split()
        inspected = json.loads(run(["docker", "inspect", cast(str, value["container"])]).stdout)
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
        raise Unknown("disposable container observation is unavailable") from error
    if ids != [value["container"]]:
        raise Failed("descriptor does not bind the exact READY container")
    if len(inspected) != 1:
        raise Failed("descriptor container identity is unavailable")
    _docker_identity(inspected[0], cast(dict[str, str], value))
    state = inspected[0].get("State")
    if not isinstance(state, dict):
        raise Failed("disposable container is not running and healthy")
    exact_state = cast(dict[str, object], state)
    health = exact_state.get("Health")
    exact_health = cast(dict[str, object], health) if isinstance(health, dict) else {}
    if exact_state.get("Running") is not True or exact_health.get("Status") != "healthy":
        raise Failed("disposable container is not running and healthy")
    if not _owned_resource(
        "network", cast(str, value["network"]), cast(str, value["project"]),
        subnet=cast(str, value["subnet"]), run=run,
    ):
        raise Failed("descriptor does not bind the exact READY network")
    return root, value


def operator_lock(name: str) -> Path:
    """Return the one validated exclusion boundary shared by operator and teardown."""
    _identity(name)
    _private_directory(REHEARSALS)
    root = REHEARSALS / name
    _private_directory(root)
    return root / "operator.lock"


def teardown(name: str, *, run: Runner = _run) -> None:
    """Remove only resources whose exact namespace is owned by the descriptor."""
    with exclusive_lock(operator_lock(name)):
        _teardown(name, run=run)


def _owned_resource(
    kind: str, resource: str, project: str, *, subnet: str | None = None, run: Runner,
) -> bool:
    list_output = ["--format", "{{.Name}}"] if kind == "network" else ["-q"]
    project_names = run([
        "docker", kind, "ls", *list_output, "--filter",
        f"label=com.docker.compose.project={project}",
    ]).stdout.split()
    exact_names = run([
        "docker", kind, "ls", *list_output, "--filter", f"name=^{re.escape(resource)}$",
    ]).stdout.split()
    if project_names not in ([], [resource]) or exact_names not in ([], [resource]) or (
        project_names != exact_names
    ):
        raise Failed(f"descriptor does not own exact {kind} inventory")
    if not exact_names:
        return False
    item = json.loads(run(["docker", kind, "inspect", resource]).stdout)[0]
    labels = cast(dict[str, object], item).get("Labels") if isinstance(item, dict) else None
    exact = cast(dict[str, object], labels) if isinstance(labels, dict) else {}
    ipam = cast(dict[str, object], item).get("IPAM") if isinstance(item, dict) else None
    configs = cast(dict[str, object], ipam).get("Config") if isinstance(ipam, dict) else None
    valid_subnet = subnet is None
    if subnet is not None and isinstance(configs, list):
        exact_configs = cast(list[object], configs)
        valid_subnet = (
            len(exact_configs) == 1
            and isinstance(exact_configs[0], dict)
            and cast(dict[str, object], exact_configs[0]).get("Subnet") == subnet
        )
    if (
        cast(dict[str, object], item).get("Name") != resource
        or exact.get("com.docker.compose.project") != project
        or exact.get(f"com.docker.compose.{kind}") != resource.removeprefix(f"{project}_")
        or not valid_subnet
    ):
        raise Failed(f"descriptor does not own {kind} {resource}")
    return True


def _teardown(name: str, *, run: Runner) -> None:
    root, value = _load(name, failed=True)
    project = cast(str, value["project"])
    try:
        ids = run([
            "docker", "ps", "-aq", "--no-trunc", "--filter", f"label=com.docker.compose.project={project}"
        ]).stdout.split()
        status = value["status"]
        bound = value.get("container")
        if (
            len(ids) > 1
            or status == "READY" and ids != [bound]
            or status == "FAILED" and bool(ids) and isinstance(bound, str) and ids != [bound]
        ):
            raise Failed("descriptor does not bind the exact container")
        if ids:
            inspected = json.loads(run(["docker", "inspect", ids[0]]).stdout)[0]
            _docker_identity(inspected, cast(dict[str, str], value))
        resources: list[tuple[str, str]] = []
        for kind in ("volume", "network"):
            resource = cast(str, value[kind])
            if _owned_resource(
                kind, resource, project,
                subnet=cast(str, value["subnet"]) if kind == "network" else None,
                run=run,
            ):
                resources.append((kind, resource))
        if ids:
            run(["docker", "rm", "-f", ids[0]])
        for kind, resource in resources:
            run(["docker", kind, "rm", resource])
    except (IndexError, json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
        raise Failed("disposable target teardown could not prove ownership") from error
    shutil.rmtree(root)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    create = subparsers.add_parser("provision")
    create.add_argument("name")
    create.add_argument("--candidate", required=True)
    create.add_argument("--runtime", required=True, type=Path)
    create.add_argument("--database-backup", required=True, type=Path)
    create.add_argument("--fastmcp-snapshot", required=True, type=Path)
    remove = subparsers.add_parser("teardown")
    remove.add_argument("name")
    arguments = parser.parse_args()
    try:
        if arguments.operation == "provision":
            print(provision(arguments.name, arguments.candidate, arguments.runtime,
                            arguments.database_backup, arguments.fastmcp_snapshot))
        else:
            teardown(arguments.name)
    except Failed as error:
        parser.exit(1, f"rehearsal target {arguments.operation} failed: {error}\n")
