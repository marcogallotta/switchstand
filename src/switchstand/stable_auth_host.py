"""Default-off host assets and private credential handling for split authentication."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AUTH_PORT = 8791
EDGE_PORT = 8790
MAX_INTERNAL_SECRET_BYTES = 256
INTERNAL_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{64}")
MIN_INTERNAL_SECRET_DISTINCT_CHARACTERS = 16
PUBLIC_MCP_PATHS = ("/switchstand/mcp", "/switchstand/mcp/*")
PUBLIC_RESOURCE_METADATA_PATHS = ("/.well-known/oauth-protected-resource/switchstand/mcp",)
PUBLIC_OAUTH_PATHS = (
    "/switchstand/authorize",
    "/switchstand/token",
    "/switchstand/register",
    "/switchstand/auth/callback",
    "/switchstand/consent",
)
PUBLIC_ISSUER_METADATA_PATHS = ("/.well-known/oauth-authorization-server/switchstand",)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_internal_secret(path: Path) -> str:
    """Read one owned, regular, mode-0600 secret without following a symlink."""
    if not path.is_absolute():
        raise ValueError("internal credential path must be absolute")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("internal credential is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("internal credential must be an owned mode-0600 regular file")
        value = os.read(descriptor, MAX_INTERNAL_SECRET_BYTES + 1)
        if not value or len(value) > MAX_INTERNAL_SECRET_BYTES:
            raise ValueError("internal credential has an invalid size")
        if os.read(descriptor, 1):
            raise ValueError("internal credential has an invalid size")
        try:
            secret = value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("internal credential must be ASCII") from exc
        if (
            INTERNAL_SECRET_PATTERN.fullmatch(secret) is None
            or len(set(secret)) < MIN_INTERNAL_SECRET_DISTINCT_CHARACTERS
        ):
            raise ValueError("internal credential has an invalid generated-token format")
        return secret
    finally:
        os.close(descriptor)


def _atomic_secret_write(path: Path, value: str, *, require_absent: bool) -> str:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("internal credential target must be absolute and symlink-free")
    if require_absent and path.exists():
        raise ValueError("internal credential already exists")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink() or parent.stat().st_uid != os.getuid():
        raise ValueError("internal credential parent must be an owned real directory")
    temporary = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    encoded = value.encode("ascii")
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written == 0:
                raise OSError("short write while provisioning internal credential")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        if require_absent and path.exists():
            raise ValueError("internal credential appeared during provisioning")
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()
    return _digest(encoded)


@contextmanager
def _credential_lock(path: Path) -> Generator[None]:
    parent = path.parent
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not parent.is_dir()
        or parent.is_symlink()
        or parent.stat().st_uid != os.getuid()
    ):
        raise ValueError("internal credential target must be absolute in an owned real directory")
    lock = path.with_name(f".{path.name}.lock")
    try:
        descriptor = os.open(
            lock,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise ValueError("internal credential lock is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("internal credential lock must be an owned mode-0600 regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def provision_internal_secret(path: Path) -> str:
    with _credential_lock(path):
        return _atomic_secret_write(path, secrets.token_urlsafe(48), require_absent=True)


def rotate_internal_secret(path: Path, expected_sha256: str) -> str:
    with _credential_lock(path):
        current = read_internal_secret(path).encode("ascii")
        if not secrets.compare_digest(_digest(current), expected_sha256):
            raise ValueError("internal credential digest is stale")
        return _atomic_secret_write(path, secrets.token_urlsafe(48), require_absent=False)


def internal_secret_receipt(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": _digest(read_internal_secret(path).encode("ascii"))}


@dataclass(frozen=True, slots=True)
class HostAssets:
    runtime_python: Path
    auth_environment_file: Path
    edge_environment_file: Path
    internal_secret_file: Path
    auth_port: int = AUTH_PORT
    edge_port: int = EDGE_PORT

    def __post_init__(self) -> None:
        for value in (
            self.runtime_python,
            self.auth_environment_file,
            self.edge_environment_file,
            self.internal_secret_file,
        ):
            if not value.is_absolute():
                raise ValueError("host asset paths must be absolute")
            if any(character.isspace() for character in str(value)):
                raise ValueError("host asset paths must not contain whitespace")
        if self.auth_environment_file == self.edge_environment_file:
            raise ValueError("auth and edge must use separate environment files")
        if self.auth_port == self.edge_port or not all(
            1 <= value <= 65535 for value in (self.auth_port, self.edge_port)
        ):
            raise ValueError("auth and edge ports must be distinct valid ports")


def caddy_routes(assets: HostAssets) -> list[dict[str, Any]]:
    """Return the exact inert route candidate; private introspection is intentionally absent."""
    def proxy(identifier: str, port: int) -> dict[str, Any]:
        return {
            "@id": identifier,
            "handler": "reverse_proxy",
            "upstreams": [{"dial": f"127.0.0.1:{port}"}],
        }

    return [
        {
            "match": [{"path": list(PUBLIC_MCP_PATHS)}],
            "handle": [
                {"handler": "rewrite", "strip_path_prefix": "/switchstand"},
                proxy("switchstand_split_mcp_proxy", assets.edge_port),
            ],
            "terminal": True,
        },
        {
            "match": [{"path": list(PUBLIC_RESOURCE_METADATA_PATHS)}],
            "handle": [proxy("switchstand_split_resource_metadata_proxy", assets.edge_port)],
            "terminal": True,
        },
        {
            "match": [{"path": list(PUBLIC_OAUTH_PATHS)}],
            "handle": [
                {"handler": "rewrite", "strip_path_prefix": "/switchstand"},
                proxy("switchstand_split_oauth_proxy", assets.auth_port),
            ],
            "terminal": True,
        },
        {
            "match": [{"path": list(PUBLIC_ISSUER_METADATA_PATHS)}],
            "handle": [
                {"handler": "rewrite", "uri": "/.well-known/oauth-authorization-server"},
                proxy("switchstand_split_issuer_metadata_proxy", assets.auth_port),
            ],
            "terminal": True,
        },
    ]


def systemd_units(assets: HostAssets) -> dict[str, str]:
    auth = (
        "[Unit]\nAfter=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"EnvironmentFile={assets.auth_environment_file}\n"
        + f"Environment=SWITCHSTAND_INTERNAL_CREDENTIAL_FILE={assets.internal_secret_file}\n"
        + f"Environment=SWITCHSTAND_AUTH_BIND_PORT={assets.auth_port}\n"
        + f"ExecStart={assets.runtime_python} -m switchstand.stable_auth_runtime auth\n"
        + "Restart=on-failure\nRestartSec=2\n\n[Install]\nWantedBy=default.target\n"
    )
    edge = (
        "[Unit]\nAfter=network-online.target switchstand-stable-auth.service\n"
        "Wants=network-online.target\nRequires=switchstand-stable-auth.service\n\n"
        "[Service]\nType=simple\n"
        f"EnvironmentFile={assets.edge_environment_file}\n"
        f"Environment=SWITCHSTAND_INTERNAL_CREDENTIAL_FILE={assets.internal_secret_file}\n"
        f"Environment=SWITCHSTAND_AUTH_INTERNAL_URL=http://127.0.0.1:{assets.auth_port}\n"
        f"Environment=SWITCHSTAND_MCP_BIND_PORT={assets.edge_port}\n"
        f"ExecStart={assets.runtime_python} -m switchstand.stable_auth_runtime edge\n"
        "Restart=on-failure\nRestartSec=2\nTimeoutStopSec=45\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    return {
        "switchstand-stable-auth.service": auth,
        "switchstand-delegated-edge.service": edge,
    }


def render_assets(assets: HostAssets, output: Path) -> dict[str, str]:
    if not output.is_absolute() or not output.is_dir() or output.is_symlink():
        raise ValueError("output must be an existing absolute real directory")
    for environment in (assets.auth_environment_file, assets.edge_environment_file):
        try:
            metadata = environment.lstat()
        except OSError as exc:
            raise ValueError("environment file is unavailable") from exc
        if (
            environment.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("environment files must be owned mode-0600 regular files")
    read_internal_secret(assets.internal_secret_file)
    rendered = systemd_units(assets)
    rendered["switchstand-split-caddy-routes.json"] = json.dumps(
        caddy_routes(assets), indent=2, sort_keys=True
    ) + "\n"
    for name in rendered:
        target = output / name
        if target.exists() or target.is_symlink():
            raise ValueError(f"refusing to replace rendered asset: {name}")
    receipts: dict[str, str] = {}
    for name, content in rendered.items():
        target = output / name
        target.write_text(content)
        receipts[name] = _digest(content.encode())
    return receipts


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("credential-init", "credential-readback"):
        child = subcommands.add_parser(command)
        child.add_argument("path", type=Path)
    rotate = subcommands.add_parser("credential-rotate")
    rotate.add_argument("path", type=Path)
    rotate.add_argument("--expected-sha256", required=True)
    render = subcommands.add_parser("render")
    render.add_argument("--runtime-python", required=True, type=Path)
    render.add_argument("--auth-environment-file", required=True, type=Path)
    render.add_argument("--edge-environment-file", required=True, type=Path)
    render.add_argument("--internal-secret-file", required=True, type=Path)
    render.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    if arguments.command == "credential-init":
        digest = provision_internal_secret(arguments.path)
        result = {"path": str(arguments.path), "sha256": digest}
    elif arguments.command == "credential-rotate":
        digest = rotate_internal_secret(arguments.path, arguments.expected_sha256)
        result = {"path": str(arguments.path), "sha256": digest}
    elif arguments.command == "credential-readback":
        result = internal_secret_receipt(arguments.path)
    else:
        assets = HostAssets(
            arguments.runtime_python,
            arguments.auth_environment_file,
            arguments.edge_environment_file,
            arguments.internal_secret_file,
        )
        result = render_assets(assets, arguments.output)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    run()
