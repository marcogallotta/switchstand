"""Fail-closed single-host activation and replacement for split authentication."""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import os
import re
import signal
import stat
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import urlparse

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .edge_maintenance import (
    CADDY,
    EDGE_PATHS,
    FASTMCP_STATE,
    GATE_ID,
    LOCK,
    PROXIES,
    PUBLIC_ORIGIN,
    Failed,
    Interrupted,
    Unknown,
    atomic_copy,
    exclusive_lock,
    run_host_command,
)
from .edge_monitor_host import ExternalIngressHttp
from .secure_file import atomic_replace_bytes
from .stable_auth import INTROSPECTION_PATH, IntrospectionContract
from .stable_auth_host import HostAssets, caddy_routes, read_internal_secret, systemd_units
from .stable_auth_migration import MigrationFailure, SystemdWriterProbe, copy_with_receipt

COMBINED_SERVICE = "switchstand-chatgpt-mcp.service"
AUTH_SERVICE = "switchstand-stable-auth.service"
EDGE_SERVICE = "switchstand-delegated-edge.service"
SPLIT_PROXIES = {
    "switchstand_split_mcp_proxy",
    "switchstand_split_resource_metadata_proxy",
    "switchstand_split_oauth_proxy",
    "switchstand_split_issuer_metadata_proxy",
}
PROXY_TRANSITIONS = {
    "switchstand_mcp_proxy": {
        "@id": "switchstand_split_mcp_proxy",
        "handler": "reverse_proxy",
        "upstreams": [{"dial": "127.0.0.1:8790"}],
    },
    "switchstand_mcp_metadata_proxy": {
        "@id": "switchstand_split_resource_metadata_proxy",
        "handler": "reverse_proxy",
        "upstreams": [{"dial": "127.0.0.1:8790"}],
    },
    "switchstand_mcp_oauth_proxy": {
        "@id": "switchstand_split_oauth_proxy",
        "handler": "reverse_proxy",
        "upstreams": [{"dial": "127.0.0.1:8791"}],
    },
    "switchstand_mcp_oauth_metadata_proxy": {
        "@id": "switchstand_split_issuer_metadata_proxy",
        "handler": "reverse_proxy",
        "upstreams": [{"dial": "127.0.0.1:8791"}],
    },
}
ROUTES_PATH = "/config/apps/http/servers/dish_action_router/routes"


def _env(path: Path) -> dict[str, str]:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise Failed("environment file must be an owned mode-0600 regular file")
        values = {
            key.strip(): value.strip().strip("'\"")
            for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#") and "=" in line
            for key, value in [line.split("=", 1)]
        }
    except (OSError, UnicodeError) as exc:
        raise Failed("environment file is unavailable") from exc
    return values


def _fastmcp_home(environment: dict[str, str]) -> Path:
    configured = environment.get("FASTMCP_HOME")
    if configured:
        return Path(configured)
    data_home = environment.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home) / "fastmcp"
    home = environment.get("HOME")
    if home:
        return Path(home) / ".local/share/fastmcp"
    raise Failed("running service FastMCP state identity is unavailable")


def _contains_id(value: object, identifiers: set[str]) -> bool:
    if isinstance(value, dict):
        candidate = cast(dict[object, object], value)
        return candidate.get("@id") in identifiers or any(
            _contains_id(item, identifiers) for item in candidate.values()
        )
    if isinstance(value, list):
        return any(_contains_id(item, identifiers) for item in cast(list[object], value))
    return False


def _replace_proxy_nodes(value: object) -> object:
    if isinstance(value, dict):
        candidate = cast(dict[str, object], value)
        identifier = candidate.get("@id")
        if isinstance(identifier, str) and identifier in PROXY_TRANSITIONS:
            return PROXY_TRANSITIONS[identifier]
        return {key: _replace_proxy_nodes(item) for key, item in candidate.items()}
    if isinstance(value, list):
        return [_replace_proxy_nodes(item) for item in cast(list[object], value)]
    return value


def _unit_state(service: str) -> tuple[str, int, int]:
    completed = run_host_command(
        [
            "systemctl",
            "--user",
            "show",
            service,
            "--property=ActiveState,MainPID,NRestarts",
        ],
        check=False,
    )
    values = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
    try:
        return (
            values.get("ActiveState", "unknown"),
            int(values.get("MainPID", "0")),
            int(values.get("NRestarts", "-1")),
        )
    except ValueError as exc:
        raise Unknown("systemd state readback is malformed") from exc


def _service(service: str, action: str, wanted: Literal["active", "inactive"]) -> None:
    run_host_command(["systemctl", "--user", action, service], check=False)
    state, pid, _restarts = _unit_state(service)
    if wanted == "active" and state == "active" and pid > 0:
        return
    if wanted == "inactive" and _inactive_exact(service, state, pid):
        return
    raise Unknown(f"{service} did not become {wanted}")


def _inactive_exact(service: str, state: str, pid: int) -> bool:
    if state in {"inactive", "failed"} and pid == 0:
        return True
    return state == "unknown" and pid == 0 and _enablement_state(service) == "not-found"


def _enablement_state(service: str) -> str:
    completed = run_host_command(["systemctl", "--user", "is-enabled", service], check=False)
    state = completed.stdout.strip()
    if state not in {"enabled", "disabled", "not-found"}:
        raise Unknown(f"{service} startup state is unreadable")
    return state


def _enabled(service: str) -> bool:
    return _enablement_state(service) == "enabled"


def _set_enabled(service: str, wanted: bool) -> None:
    run_host_command(["systemctl", "--user", "enable" if wanted else "disable", service], check=False)
    if _enabled(service) != wanted:
        raise Failed(f"{service} startup state did not change")


def _atomic_json(path: Path, value: object, *, indent: int | None = None) -> None:
    encoded = (json.dumps(value, indent=indent, sort_keys=True) + "\n").encode()
    atomic_replace_bytes(path, encoded)


class CaddyRoutes:
    def __init__(
        self,
        *,
        endpoint: str,
        public_origin: str,
        gate_id: str,
        paths: tuple[str, ...],
        retry_after: int,
    ):
        self.endpoint = endpoint
        self.public_origin = public_origin
        self.gate_id = gate_id
        self.paths = paths
        self.retry_after = retry_after

    @property
    def gate_route(self) -> dict[str, object]:
        return {
            "@id": self.gate_id,
            "match": [{"path": list(self.paths)}],
            "handle": [
                {
                    "handler": "static_response",
                    "status_code": "503",
                    "headers": {"Retry-After": [str(self.retry_after)]},
                }
            ],
            "terminal": True,
        }

    def api(self, method: str, path: str, body: object | None = None) -> object | None:
        request = urllib.request.Request(
            self.endpoint + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and method == "GET":
                return None
            if method == "GET":
                raise Unknown("Caddy state unreadable") from exc
            raise Failed(f"Caddy {method} failed") from exc
        except (OSError, TimeoutError) as exc:
            raise Unknown("Caddy state unreadable") from exc
        try:
            return json.loads(raw) if raw else None
        except (UnicodeError, ValueError) as exc:
            raise Unknown("Caddy readback is invalid") from exc

    def routes(self) -> list[object]:
        value = self.api("GET", ROUTES_PATH)
        if not isinstance(value, list):
            raise Unknown("Caddy route set is unreadable")
        return cast(list[object], value)

    def gate_exact(self) -> bool:
        return self.api("GET", f"/id/{self.gate_id}") == self.gate_route

    def gate(self) -> None:
        if self.gate_exact():
            return
        try:
            self.api("PUT", ROUTES_PATH + "/0", self.gate_route)
        except Interrupted:
            raise
        except Failed, Unknown:
            if self.gate_exact():
                return
            raise Unknown("maintenance gate insertion is ambiguous") from None
        if not self.gate_exact():
            raise Unknown("maintenance gate readback mismatch")

    def transition_proxies(self, *, reverse: bool = False) -> None:
        for old_identifier, split in PROXY_TRANSITIONS.items():
            split_identifier = cast(str, split["@id"])
            old = {
                "@id": old_identifier,
                "handler": "reverse_proxy",
                "upstreams": [{"dial": "127.0.0.1:8790"}],
            }
            source_id, source, target_id, target = (
                (split_identifier, split, old_identifier, old)
                if reverse
                else (old_identifier, old, split_identifier, split)
            )
            observed_source = self.api("GET", f"/id/{source_id}")
            observed_target = self.api("GET", f"/id/{target_id}")
            if observed_source is None and observed_target == target:
                continue
            if observed_source != source or observed_target is not None:
                raise Unknown("Caddy proxy transition subject is ambiguous")
            try:
                self.api("PATCH", f"/id/{source_id}", target)
            except Interrupted:
                raise
            except Failed, Unknown:
                reconciled_source = self.api("GET", f"/id/{source_id}")
                reconciled_target = self.api("GET", f"/id/{target_id}")
                if reconciled_source is None and reconciled_target == target:
                    continue
                if reconciled_source == source and reconciled_target is None:
                    raise Failed("Caddy proxy transition did not take effect") from None
                raise Unknown("Caddy proxy transition is ambiguous") from None
            if (
                self.api("GET", f"/id/{source_id}") is not None
                or self.api("GET", f"/id/{target_id}") != target
            ):
                raise Unknown("Caddy proxy transition readback mismatch")

    def ungate(self) -> None:
        if not self.gate_exact():
            raise Unknown("maintenance gate changed before removal")
        try:
            self.api("DELETE", f"/id/{self.gate_id}")
        except Interrupted:
            raise
        except Failed, Unknown:
            if not self.gate_exact():
                return
            raise Unknown("maintenance gate removal is ambiguous") from None
        if self.gate_exact():
            raise Unknown("maintenance gate removal readback mismatch")

    def public_gated(self) -> bool:
        try:
            parsed = urlparse(self.public_origin)
            port = parsed.port
        except ValueError:
            return False
        if parsed.scheme == "https" and parsed.hostname and port in (None, 443):
            probe = ExternalIngressHttp(
                self.public_origin + "/switchstand/mcp",
                self.public_origin + "/switchstand/mcp",
            )
            try:
                addresses = probe.public_addresses(parsed.hostname)
                for address in addresses:
                    for path in self.paths:
                        probe_path = (
                            path[:-1] + "maintenance-probe" if path.endswith("/*") else path
                        )
                        status, headers, _body = probe.request(
                            parsed.hostname, address, "GET", probe_path
                        )
                        if status != 503 or headers.get("retry-after") != str(self.retry_after):
                            return False
                return True
            except OSError, ValueError, UnicodeError, http.client.HTTPException:
                return False
        for path in self.paths:
            probe = path[:-1] + "maintenance-probe" if path.endswith("/*") else path
            try:
                urllib.request.urlopen(self.public_origin + probe, timeout=5)
            except urllib.error.HTTPError as exc:
                if exc.code == 503 and exc.headers.get("Retry-After") == str(self.retry_after):
                    continue
            except OSError, TimeoutError:
                pass
            return False
        return True


@dataclass(frozen=True, slots=True)
class ActivationConfig:
    attempt_dir: Path
    current_runtime: Path
    current_sha: str
    candidate_runtime: Path
    candidate_sha: str
    runtime_python: Path
    assets_dir: Path
    auth_environment_file: Path
    edge_environment_file: Path
    doctor_environment_file: Path
    internal_secret_file: Path
    legacy_token_file: Path
    current_state: Path
    migrated_state: Path
    backup_state: Path
    unit_dir: Path
    retry_after: int = 60
    caddy: str = CADDY
    public_origin: str = PUBLIC_ORIGIN


class ActivationOperations(Protocol):
    def preflight(self) -> None: ...
    def gate(self) -> None: ...
    def gate_exact(self) -> bool: ...
    def public_gated(self) -> bool: ...
    def stop_combined(self) -> None: ...
    def prove_no_unknown_effects(self) -> None: ...
    def copy_state(self) -> None: ...
    def install_split(self) -> None: ...
    def start_auth(self) -> None: ...
    def auth_ready(self) -> bool: ...
    def start_edge(self) -> None: ...
    def edge_ready(self) -> bool: ...
    def route_split(self) -> None: ...
    def ungate(self) -> None: ...
    def public_ready(self) -> bool: ...
    def rollback_pre_exposure(self) -> bool: ...


class ActivationReceipt:
    def __init__(self, config: ActivationConfig):
        self.path = config.attempt_dir / "activation-receipt.json"
        self.value: dict[str, object] = {
            "schema": "switchstand.stable-auth-activation.v1",
            "current_sha": config.current_sha,
            "candidate_sha": config.candidate_sha,
            "phase": "PREFLIGHT",
            "status": "RUNNING",
            "automatic_oauth_restore": False,
        }

    def write(self, phase: str, status: str = "RUNNING", error: str | None = None) -> None:
        self.value.update(phase=phase, status=status)
        if error is not None:
            self.value["error"] = error
        _atomic_json(self.path, self.value)


def activate(config: ActivationConfig, operations: ActivationOperations) -> str:
    receipt = ActivationReceipt(config)
    phase = "PREFLIGHT"
    gate_attempted = False
    receipt.write(phase)
    try:
        operations.preflight()
        gate_attempted = True
        operations.gate()
        phase = "GATED"
        receipt.write(phase)
        if not operations.public_gated():
            raise Unknown("public maintenance gate is not exact")
        operations.stop_combined()
        phase = "STOPPED"
        receipt.write(phase)
        operations.prove_no_unknown_effects()
        phase = "EFFECTS_CLEAR"
        receipt.write(phase)
        operations.copy_state()
        phase = "STATE_COPIED"
        receipt.write(phase)
        operations.install_split()
        phase = "SPLIT_INSTALLED"
        receipt.write(phase)
        operations.start_auth()
        phase = "AUTH_STARTED"
        receipt.write(phase)
        if not operations.auth_ready():
            raise Failed("stable authorization local verification failed")
        operations.start_edge()
        phase = "EDGE_STARTED"
        receipt.write(phase)
        if not operations.edge_ready():
            raise Failed("delegated edge local verification failed")
        operations.route_split()
        phase = "SPLIT_ROUTED"
        receipt.write(phase)
        operations.ungate()
        phase = "UNGATED"
        receipt.write(phase)
        if not operations.public_ready():
            raise Unknown("split public verification failed after possible OAuth writes")
        receipt.write("COMPLETE", "PASS")
        return "PASS"
    except Unknown as exc:
        error = type(exc).__name__
        try:
            if gate_attempted and not operations.gate_exact():
                operations.gate()
        except Failed, Unknown, OSError, subprocess.SubprocessError:
            error = "GateRetentionUnknown"
        receipt.write(phase, "UNKNOWN", error)
        return "UNKNOWN"
    except (
        Failed,
        MigrationFailure,
        OSError,
        UnicodeError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        try:
            if phase != "PREFLIGHT" and not operations.rollback_pre_exposure():
                raise Unknown("combined rollback verification failed")
        except Failed, Unknown, OSError, subprocess.SubprocessError:
            try:
                if not operations.gate_exact():
                    operations.gate()
                error = "RollbackUnknown"
            except Failed, Unknown, OSError, subprocess.SubprocessError:
                error = "GateRetentionUnknown"
            receipt.write(phase, "UNKNOWN", error)
            return "UNKNOWN"
        receipt.write("ROLLED_BACK", "FAIL", type(exc).__name__)
        return "FAIL"


class HostActivationOperations:
    def __init__(self, config: ActivationConfig):
        self.c = config
        self.caddy = CaddyRoutes(
            endpoint=config.caddy,
            public_origin=config.public_origin,
            gate_id=GATE_ID,
            paths=EDGE_PATHS,
            retry_after=config.retry_after,
        )
        self.routes_before: list[object] | None = None
        self.split_routes = cast(
            list[object],
            caddy_routes(
                HostAssets(
                    config.runtime_python,
                    config.candidate_runtime,
                    config.auth_environment_file,
                    config.edge_environment_file,
                    config.internal_secret_file,
                )
            ),
        )
        self.database_url: str | None = None

    def _process_environment(self, service: str) -> dict[str, str]:
        state, pid, _restarts = _unit_state(service)
        if state != "active" or pid <= 0:
            raise Failed(f"{service} is not active")
        try:
            return {
                os.fsdecode(key): os.fsdecode(value)
                for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                if b"=" in item
                for key, value in [item.split(b"=", 1)]
            }
        except (OSError, UnicodeError) as exc:
            raise Unknown("running service environment is unreadable") from exc

    def _running_split_exact(self, service: str, role: str) -> bool:
        state, pid, _restarts = _unit_state(service)
        if state != "active" or pid <= 0:
            raise Unknown(f"{service} process is unavailable")
        try:
            arguments = [
                item for item in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if item
            ]
            environment = self._process_environment(service)
        except (OSError, UnicodeError) as exc:
            raise Unknown(f"{service} process identity is unreadable") from exc
        return arguments == [
            os.fsencode(self.c.runtime_python),
            b"-m",
            b"switchstand.stable_auth_runtime",
            role.encode(),
        ] and environment.get("PYTHONPATH") == str(self.c.candidate_runtime / "src")

    def preflight(self) -> None:
        c = self.c
        paths = (
            c.attempt_dir,
            c.current_runtime,
            c.candidate_runtime,
            c.assets_dir,
            c.auth_environment_file,
            c.edge_environment_file,
            c.doctor_environment_file,
            c.internal_secret_file,
            c.legacy_token_file,
            c.current_state,
            c.migrated_state.parent,
            c.backup_state.parent,
            c.unit_dir,
        )
        if any(not path.is_absolute() or path.is_symlink() for path in paths):
            raise Failed("activation paths must be absolute and symlink-free")
        for directory in (
            c.attempt_dir,
            c.current_runtime,
            c.candidate_runtime,
            c.assets_dir,
            c.current_state,
            c.migrated_state.parent,
            c.backup_state.parent,
            c.unit_dir,
        ):
            if not directory.is_dir():
                raise Failed("activation directory layout is unavailable")
        attempt_metadata = c.attempt_dir.stat()
        if (
            not stat.S_ISDIR(attempt_metadata.st_mode)
            or attempt_metadata.st_uid != os.getuid()
            or stat.S_IMODE(attempt_metadata.st_mode) != 0o700
        ):
            raise Failed("attempt directory must be owned mode-0700")
        if (
            not c.runtime_python.is_absolute()
            or not c.runtime_python.is_file()
            or not os.access(c.runtime_python, os.X_OK)
        ):
            raise Failed("runtime Python must be an absolute executable file")
        if c.current_state != FASTMCP_STATE:
            raise Failed("current OAuth state is not the production store")
        if c.current_runtime == c.candidate_runtime or c.current_sha == c.candidate_sha:
            raise Failed("current and candidate runtime identities must differ")
        if c.migrated_state.exists() or c.backup_state.exists():
            raise Failed("state copy targets must be absent")
        for runtime, expected in (
            (c.current_runtime, c.current_sha),
            (c.candidate_runtime, c.candidate_sha),
        ):
            head = run_host_command(["git", "-C", str(runtime), "rev-parse", "HEAD"]).stdout.strip()
            dirty = run_host_command(["git", "-C", str(runtime), "status", "--porcelain"]).stdout
            if head != expected or dirty:
                raise Failed("runtime checkout is not the exact clean SHA")
        assets = HostAssets(
            c.runtime_python,
            c.candidate_runtime,
            c.auth_environment_file,
            c.edge_environment_file,
            c.internal_secret_file,
        )
        rendered = systemd_units(assets)
        for name, expected in rendered.items():
            candidate = c.assets_dir / name
            if not candidate.is_file() or candidate.read_text() != expected:
                raise Failed("rendered systemd asset is not exact")
            if (c.unit_dir / name).exists():
                raise Failed("split systemd unit already exists")
        route_file = c.assets_dir / "switchstand-split-caddy-routes.json"
        if json.loads(route_file.read_text()) != self.split_routes:
            raise Failed("rendered Caddy route asset is not exact")
        auth_values = _env(c.auth_environment_file)
        edge_values = _env(c.edge_environment_file)
        _env(c.doctor_environment_file)
        if auth_values.get("FASTMCP_HOME") != str(c.migrated_state):
            raise Failed("stable auth environment does not bind migrated OAuth state")
        if any(
            edge_values.get(name)
            for name in (
                "FASTMCP_HOME",
                "SWITCHSTAND_MCP_GITHUB_CLIENT_ID",
                "SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET",
            )
        ):
            raise Failed("delegated edge environment contains stable-auth state")
        if any(
            values.get(name)
            for values in (auth_values, edge_values)
            for name in ("PYTHONPATH", "PYTHONHOME")
        ):
            raise Failed("service environment overrides the candidate runtime")
        read_internal_secret(c.internal_secret_file)
        token_metadata = c.legacy_token_file.lstat()
        if (
            not stat.S_ISREG(token_metadata.st_mode)
            or token_metadata.st_uid != os.getuid()
            or stat.S_IMODE(token_metadata.st_mode) != 0o600
            or not c.legacy_token_file.read_text().strip()
        ):
            raise Failed("legacy token probe file is invalid")
        running = self._process_environment(COMBINED_SERVICE)
        if running.get("PYTHONPATH") != str(c.current_runtime / "src"):
            raise Failed("combined service does not execute the exact current runtime")
        if _fastmcp_home(running) != c.current_state:
            raise Failed("combined service does not own the expected OAuth state")
        if running.get("SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET") != auth_values.get(
            "SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET"
        ):
            raise Failed("candidate signing material differs from the running edge")
        self.database_url = running.get("DATABASE_URL")
        if not self.database_url:
            raise Failed("running edge database identity is unavailable")
        for service in (AUTH_SERVICE, EDGE_SERVICE):
            state, pid, _restarts = _unit_state(service)
            if not _inactive_exact(service, state, pid):
                raise Failed("split services are not affirmatively inactive")
        if not _enabled(COMBINED_SERVICE) or _enabled(AUTH_SERVICE) or _enabled(EDGE_SERVICE):
            raise Failed("service startup ownership is not the combined unit")
        routes = self.caddy.routes()
        if self.caddy.gate_exact():
            raise Failed("maintenance gate already exists")
        if sum(_contains_id(route, PROXIES) for route in routes) != 4:
            raise Failed("combined Caddy proxy set is not exact")
        for proxy in PROXIES:
            value = self.caddy.api("GET", f"/id/{proxy}")
            if (
                not isinstance(value, dict)
                or cast(dict[str, object], value).get("handler") != "reverse_proxy"
                or cast(dict[str, object], value).get("upstreams") != [{"dial": "127.0.0.1:8790"}]
            ):
                raise Failed("combined Caddy proxy target is not exact")
        if any(_contains_id(route, SPLIT_PROXIES) for route in routes):
            raise Failed("split Caddy routes already exist")
        transformed = [
            _replace_proxy_nodes(route) for route in routes if _contains_id(route, PROXIES)
        ]
        if transformed != self.split_routes:
            raise Failed("combined Caddy routes do not match the generated split candidate")
        self.routes_before = routes
        backup = c.attempt_dir / "caddy.before.json"
        _atomic_json(backup, routes, indent=2)

    def gate(self) -> None:
        if self.routes_before is None:
            raise Failed("activation preflight was not completed")
        self.caddy.gate()

    def gate_exact(self) -> bool:
        return self.caddy.gate_exact()

    def public_gated(self) -> bool:
        return self.caddy.public_gated()

    def stop_combined(self) -> None:
        _service(COMBINED_SERVICE, "stop", "inactive")

    def prove_no_unknown_effects(self) -> None:
        database_url = self.database_url
        if database_url is None:
            raise Failed("database identity was not captured")

        async def count() -> int:
            engine = create_async_engine(database_url)
            try:
                async with engine.connect() as connection:
                    value = await connection.scalar(
                        text(
                            "SELECT count(*) FROM effect_intents "
                            "WHERE outcome->>'effect' = 'unknown'"
                        )
                    )
                    return int(value or 0)
            finally:
                await engine.dispose()

        try:
            unresolved = asyncio.run(count())
        except Exception as exc:
            raise Unknown("unresolved-effect proof unavailable") from exc
        if unresolved:
            raise Failed("unresolved durable effects block activation")

    def copy_state(self) -> None:
        secret = _env(self.c.auth_environment_file).get("SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET", "")
        probe = SystemdWriterProbe()
        try:
            copy_with_receipt(
                kind="backup",
                source=self.c.current_state,
                target=self.c.backup_state,
                receipt_path=self.c.attempt_dir / "oauth-backup-receipt.json",
                lock_path=self.c.attempt_dir / "oauth-copy.lock",
                signing_material=secret,
                probe=probe,
            )
            copy_with_receipt(
                kind="migration",
                source=self.c.current_state,
                target=self.c.migrated_state,
                receipt_path=self.c.attempt_dir / "oauth-migration-receipt.json",
                lock_path=self.c.attempt_dir / "oauth-copy.lock",
                signing_material=secret,
                probe=probe,
            )
        except MigrationFailure as exc:
            raise Failed("OAuth state copy failed") from exc

    def install_split(self) -> None:
        for name in (AUTH_SERVICE, EDGE_SERVICE):
            atomic_copy(self.c.assets_dir / name, self.c.unit_dir / name, 0o600)
        run_host_command(["systemctl", "--user", "daemon-reload"])
        _set_enabled(COMBINED_SERVICE, False)
        _set_enabled(AUTH_SERVICE, True)
        _set_enabled(EDGE_SERVICE, True)

    def start_auth(self) -> None:
        _service(AUTH_SERVICE, "start", "active")
        if _unit_state(AUTH_SERVICE)[2] != 0:
            raise Failed("stable auth restarted during activation")
        if not self._running_split_exact(AUTH_SERVICE, "auth"):
            raise Failed("stable auth process identity is not exact")

    def auth_ready(self) -> bool:
        try:
            token = self.c.legacy_token_file.read_text().strip()
            secret = read_internal_secret(self.c.internal_secret_file)
            environment = _env(self.c.auth_environment_file)
            contract = IntrospectionContract.for_resource(
                environment["SWITCHSTAND_MCP_RESOURCE_URL"],
                environment["SWITCHSTAND_MCP_GITHUB_USER_ID"],
            )
            response = httpx.post(
                "http://127.0.0.1:8791" + INTROSPECTION_PATH,
                headers={"Authorization": f"Bearer {secret}"},
                json={"token": token},
                timeout=5,
                trust_env=False,
            )
            raw_payload: object = response.json()
        except KeyError, httpx.HTTPError, ValueError:
            return False
        if not isinstance(raw_payload, dict):
            return False
        payload = cast(dict[str, object], raw_payload)
        return (
            response.status_code == 200
            and set(payload)
            == {"client_id", "scopes", "subject", "expires_at", "resource", "issuer"}
            and payload.get("subject") == contract.github_user_id
            and payload.get("issuer") == contract.issuer_url
            and payload.get("resource") == contract.resource_url
            and payload.get("scopes") == list(contract.scopes)
            and isinstance(payload.get("client_id"), str)
            and bool(payload.get("client_id"))
            and type(payload.get("expires_at")) is int
        )

    def start_edge(self) -> None:
        _service(EDGE_SERVICE, "start", "active")
        if _unit_state(EDGE_SERVICE)[2] != 0:
            raise Failed("delegated edge restarted during activation")
        if not self._running_split_exact(EDGE_SERVICE, "edge"):
            raise Failed("delegated edge process identity is not exact")

    def _doctor(self, runtime: Path, expected_sha: str, public: bool) -> bool:
        local_url = "http://127.0.0.1:8790/mcp"
        command = [
            str(runtime / "scripts/switchstand-edge-doctor"),
            "--env-file",
            str(self.c.doctor_environment_file),
            "--repo",
            str(runtime),
            "--expected-sha",
            expected_sha,
            "--local-url",
            local_url,
            "--public-url",
            self.c.public_origin.rstrip("/") + "/switchstand/mcp" if public else local_url,
        ]
        return run_host_command(command, check=False).returncode == 0

    def edge_ready(self) -> bool:
        return self._doctor(self.c.candidate_runtime, self.c.candidate_sha, False)

    def route_split(self) -> None:
        if self.routes_before is None:
            raise Failed("activation route backup is unavailable")
        self.caddy.transition_proxies()

    def ungate(self) -> None:
        self.caddy.ungate()

    def public_ready(self) -> bool:
        if (
            _unit_state(AUTH_SERVICE)[2] != 0
            or _unit_state(EDGE_SERVICE)[2] != 0
            or _enabled(COMBINED_SERVICE)
            or not _enabled(AUTH_SERVICE)
            or not _enabled(EDGE_SERVICE)
            or not self._running_split_exact(AUTH_SERVICE, "auth")
            or not self._running_split_exact(EDGE_SERVICE, "edge")
        ):
            return False
        if not self._doctor(self.c.candidate_runtime, self.c.candidate_sha, True):
            return False
        try:
            request = urllib.request.Request(
                self.c.public_origin + INTROSPECTION_PATH,
                data=b"{}",
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            return exc.code == 404
        except OSError, TimeoutError:
            return False
        return False

    def rollback_pre_exposure(self) -> bool:
        if self.routes_before is None:
            return False
        if not self.caddy.gate_exact():
            return False
        for service in (EDGE_SERVICE, AUTH_SERVICE):
            _service(service, "stop", "inactive")
        self.caddy.transition_proxies(reverse=True)
        _set_enabled(EDGE_SERVICE, False)
        _set_enabled(AUTH_SERVICE, False)
        _set_enabled(COMBINED_SERVICE, True)
        for name in (AUTH_SERVICE, EDGE_SERVICE):
            (self.c.unit_dir / name).unlink(missing_ok=True)
        run_host_command(["systemctl", "--user", "daemon-reload"])
        _service(COMBINED_SERVICE, "start", "active")
        running = self._process_environment(COMBINED_SERVICE)
        if (
            running.get("PYTHONPATH") != str(self.c.current_runtime / "src")
            or _fastmcp_home(running) != self.c.current_state
        ):
            return False
        if not self._doctor(self.c.current_runtime, self.c.current_sha, False):
            return False
        self.caddy.ungate()
        return _unit_state(COMBINED_SERVICE)[0] == "active" and self._doctor(
            self.c.current_runtime, self.c.current_sha, True
        )


def _config(arguments: argparse.Namespace) -> ActivationConfig:
    values = vars(arguments).copy()
    values.pop("command", None)
    return ActivationConfig(**values)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    activate_parser = subcommands.add_parser("activate")
    for name in (
        "attempt-dir",
        "current-runtime",
        "candidate-runtime",
        "runtime-python",
        "assets-dir",
        "auth-environment-file",
        "edge-environment-file",
        "doctor-environment-file",
        "internal-secret-file",
        "legacy-token-file",
        "current-state",
        "migrated-state",
        "backup-state",
        "unit-dir",
    ):
        activate_parser.add_argument("--" + name, type=Path, required=True)
    activate_parser.add_argument("--current-sha", required=True)
    activate_parser.add_argument("--candidate-sha", required=True)
    activate_parser.add_argument("--retry-after", type=int, default=60)
    activate_parser.add_argument("--caddy", default=CADDY)
    activate_parser.add_argument("--public-origin", default=PUBLIC_ORIGIN)
    arguments = parser.parse_args(argv)
    if (
        not re.fullmatch(r"[0-9a-f]{40}", arguments.current_sha)
        or not re.fullmatch(r"[0-9a-f]{40}", arguments.candidate_sha)
        or arguments.retry_after < 1
    ):
        parser.error("candidate SHAs or retry interval are invalid")
    arguments.attempt_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    config = _config(arguments)
    with exclusive_lock(LOCK):

        def interrupted(signum: int, _frame: object) -> None:
            raise Interrupted(f"activation interrupted by signal {signum}")

        previous_int = signal.signal(signal.SIGINT, interrupted)
        previous_term = signal.signal(signal.SIGTERM, interrupted)
        try:
            result = activate(config, HostActivationOperations(config))
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
    print(result)
    return {"PASS": 0, "FAIL": 1, "UNKNOWN": 2}[result]


if __name__ == "__main__":
    raise SystemExit(run())
