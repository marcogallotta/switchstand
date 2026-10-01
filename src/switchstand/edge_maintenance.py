"""Fail-closed maintenance-window replacement of the production ChatGPT edge."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TextIO, cast
from urllib.parse import urlparse

from .edge_monitor_host import ExternalIngressHttp
from .secure_file import atomic_replace_bytes

SERVICE = "switchstand-chatgpt-mcp.service"
CADDY = "http://127.0.0.1:2019"
PUBLIC_ORIGIN = "https://laptop.tail46f0b9.ts.net"
LOCAL_URL = "http://127.0.0.1:8790/mcp"
EDGE_PATHS = (
    "/switchstand/mcp",
    "/switchstand/mcp/*",
    "/.well-known/oauth-protected-resource/switchstand/mcp",
    "/switchstand/authorize",
    "/switchstand/token",
    "/switchstand/register",
    "/switchstand/auth/callback",
    "/switchstand/consent",
    "/.well-known/oauth-authorization-server/switchstand",
)
PROXIES = {
    "switchstand_mcp_proxy",
    "switchstand_mcp_metadata_proxy",
    "switchstand_mcp_oauth_proxy",
    "switchstand_mcp_oauth_metadata_proxy",
}
GATE_ID = "switchstand_edge_maintenance"
LOCK = Path("/home/marco/.local/state/switchstand/edge-maintenance.lock")
FASTMCP_STATE = Path("/home/marco/.local/share/fastmcp")
REHEARSALS = Path("/home/marco/.local/state/switchstand/rehearsals")


class Failed(RuntimeError):
    """A definite failure which can be rolled back."""


class Unknown(RuntimeError):
    """An effect whose state cannot be established."""


class Interrupted(Unknown):
    """An operator interrupt which mutation reconciliation must not suppress."""


class GateRetentionUnknown(Unknown):
    """The safety gate could not be installed and proven exact."""


@dataclass(frozen=True)
class Config:
    attempt_dir: Path
    current_runtime: Path
    current_sha: str
    candidate_runtime: Path
    candidate_sha: str
    candidate_launcher: Path
    candidate_launcher_sha: str
    launcher: Path
    current_launcher_sha: str
    fastmcp_state: Path
    env_file: Path
    target: Literal["production", "disposable"]
    retry_after: int = 60
    caddy: str = CADDY
    public_origin: str = PUBLIC_ORIGIN
    target_root: Path | None = None
    service: str = SERVICE
    local_url: str = LOCAL_URL
    lock_path: Path = LOCK


class Operations(Protocol):
    def preflight(self) -> None: ...
    def gate(self) -> None: ...
    def gate_exact(self) -> bool: ...
    def public_gated(self) -> bool: ...
    def stop(self) -> None: ...
    def snapshot(self) -> None: ...
    def swap(self) -> None: ...
    def start(self) -> None: ...
    def local_ready(self) -> bool: ...
    def rollback_ready(self) -> bool: ...
    def ungate(self) -> None: ...
    def public_ready(self) -> bool: ...
    def restore_launcher(self) -> None: ...
    def snapshot_digest(self) -> str: ...
    def reconcile_phase(
        self, phase: str, proof: dict[str, object], *, offline: bool
    ) -> tuple[str, dict[str, str]]: ...


class OfflineStep(Protocol):
    receipt_path: Path
    candidate_sha: str
    database_backup: str
    corpus_manifests: tuple[str, str]
    worksheet: str

    def reconcile_boundary(self) -> str: ...
    def run(self, advance: Callable[[str], None]) -> None: ...
    def abort_pre_authority(self) -> None: ...


OFFLINE_PHASES = {
    "PRE_MARKER": "OFFLINE_PRE_MARKER",
    "POSTGRES_AUTHORITY": "OFFLINE_POSTGRES_AUTHORITY",
    "COMPLETE": "OFFLINE_COMPLETE",
}


def _offline_boundary(step: OfflineStep, config: Config) -> str:
    try:
        boundary = step.reconcile_boundary()
    except (Failed, OSError, UnicodeError, ValueError) as exc:
        raise Unknown("offline receipt reconciliation failed") from exc
    if boundary not in OFFLINE_PHASES or step.candidate_sha != config.candidate_sha:
        raise Unknown("offline receipt boundary is unknown")
    return boundary


def _sha(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def run_host_command(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one bounded host command for a maintenance operation."""
    return subprocess.run(command, check=check, capture_output=True, text=True, timeout=45)


def validate_target(config: Config) -> None:
    """Reject any maintenance target whose host identity is not exact."""
    if config.target == "production":
        if config.target_root is not None or (
            config.service, config.fastmcp_state, config.local_url,
            config.caddy, config.public_origin, config.lock_path,
        ) != (SERVICE, FASTMCP_STATE, LOCAL_URL, CADDY, PUBLIC_ORIGIN, LOCK):
            raise Failed("production target identity is not exact")
        return
    if config.target != "disposable":
        raise Failed("maintenance target is unknown")
    root = config.target_root
    parent = REHEARSALS
    try:
        invalid_root = root is None or root.parent != parent or any(
            not path.is_absolute()
            or not path.is_dir()
            or path.resolve(strict=True) != path
            or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o077
            for path in (parent, root)
        )
    except OSError as error:
        raise Failed("disposable target root is not exact") from error
    if invalid_root or root is None:
        raise Failed("disposable target root is not exact")
    mutable = (
        config.attempt_dir, config.fastmcp_state, config.launcher,
        config.candidate_launcher, config.env_file, config.lock_path,
        config.attempt_dir / "receipt.json",
        config.attempt_dir / "launcher.before",
        config.attempt_dir / "fastmcp.before.tar",
        config.launcher.with_name(f".{config.launcher.name}.maintenance.tmp"),
    )
    if any(
        path.resolve(strict=False) != path
        or not path.resolve(strict=False).is_relative_to(root)
        for path in mutable
    ):
        raise Failed("disposable target path escapes its private root")
    endpoints = (config.caddy, config.local_url, config.public_origin)
    try:
        parsed = tuple(urlparse(value) for value in endpoints)
        ports = {value.port for value in parsed}
        live_ports = {urlparse(value).port for value in (CADDY, LOCAL_URL)}
    except ValueError as error:
        raise Failed("disposable target endpoint is invalid") from error
    if (
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", root.name)
        or config.service != f"switchstand-rehearsal-{root.name}.service"
        or any(value in {CADDY, LOCAL_URL, PUBLIC_ORIGIN} for value in endpoints)
        or any(value.scheme != "http" or value.hostname != "127.0.0.1" or not value.port for value in parsed)
        or not ports.isdisjoint(live_ports)
        or len(ports) != 3
    ):
        raise Failed("disposable target resolves a live or non-isolated identity")


def atomic_copy(source: Path, target: Path, mode: int) -> None:
    """Durably replace one host file with explicit permissions."""
    temporary = target.with_name(f".{target.name}.maintenance.tmp")
    shutil.copy2(source, temporary, follow_symlinks=False)
    os.chmod(temporary, mode)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _validate_launch_mapping(config: Config) -> None:
    current = config.launcher.read_bytes()
    old_runtime = os.fsencode(config.current_runtime)
    if current.count(old_runtime) != 1:
        raise Failed("current launcher does not bind exactly one runtime")
    expected = current.replace(old_runtime, os.fsencode(config.candidate_runtime), 1)
    if config.candidate_launcher.read_bytes() != expected:
        raise Failed("candidate launcher is not the exact runtime retarget")


def exclusive_lock(path: Path = LOCK) -> TextIO:
    """Acquire the non-blocking host-maintenance lock."""
    try:
        fd = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise SystemExit("edge maintenance lock is unavailable") from exc
    handle = os.fdopen(fd, "a")
    stat = os.fstat(fd)
    if stat.st_uid != os.getuid():
        handle.close()
        raise SystemExit("edge maintenance lock has the wrong owner")
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SystemExit("another edge maintenance attempt holds the lock") from None
    return handle


class HostOperations:
    def __init__(self, config: Config):
        self.c = config
        self.backup = config.attempt_dir / "launcher.before"
        self.snapshot_file = config.attempt_dir / "fastmcp.before.tar"

    def _api(self, method: str, path: str, body: object | None = None) -> object | None:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.c.caddy + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and method == "GET":
                return None
            raise Failed(f"Caddy {method} failed") from exc
        except (OSError, TimeoutError) as exc:
            raise Unknown("Caddy state unreadable") from exc
        try:
            return json.loads(raw) if raw else None
        except (UnicodeError, ValueError) as exc:
            raise Unknown("Caddy readback is invalid") from exc

    def preflight(self) -> None:
        c = self.c
        validate_target(c)
        for path in (
            c.attempt_dir,
            c.current_runtime,
            c.candidate_runtime,
            c.candidate_launcher,
            c.launcher,
            c.fastmcp_state,
            c.env_file,
        ):
            if not path.is_absolute() or path.is_symlink():
                raise Failed("preflight path is not absolute and symlink-free")
        if (
            not c.candidate_runtime.is_dir()
            or not c.fastmcp_state.is_dir()
            or not c.candidate_launcher.is_file()
            or not c.launcher.is_file()
            or not c.env_file.is_file()
            or c.env_file.stat().st_mode & 0o777 != 0o600
        ):
            raise Failed("preflight file layout or mode mismatch")
        if any(path.exists() for path in (self.backup, self.snapshot_file)):
            raise Failed("attempt artifacts already exist")
        if (
            _sha(c.launcher) != c.current_launcher_sha
            or _sha(c.candidate_launcher) != c.candidate_launcher_sha
        ):
            raise Failed("launcher digest mismatch")
        _validate_launch_mapping(c)
        for runtime, expected in (
            (c.current_runtime, c.current_sha),
            (c.candidate_runtime, c.candidate_sha),
        ):
            head = run_host_command(["git", "-C", str(runtime), "rev-parse", "HEAD"]).stdout.strip()
            dirty = run_host_command(["git", "-C", str(runtime), "status", "--porcelain"]).stdout
            if head != expected or dirty:
                raise Failed("runtime checkout is not the exact clean SHA")
            if '"fastmcp-slim[server]==4.0.3"' not in (runtime / "pyproject.toml").read_text():
                raise Failed("runtime does not pin the approved FastMCP version")
        unit = run_host_command(
            [
                "systemctl",
                "--user",
                "show",
                c.service,
                "--property=ExecStart",
                "--value",
            ]
        ).stdout
        if str(c.launcher) not in unit:
            raise Failed("edge unit does not execute the selected launcher")
        if (
            run_host_command(["systemctl", "--user", "is-active", c.service], check=False).stdout.strip()
            != "active"
        ):
            raise Failed("edge service is not active")
        if not self._running_process_exact():
            raise Failed("running edge launcher or FastMCP state is not exact")
        if self.gate_exact():
            raise Failed("maintenance gate already exists")
        local = urlparse(c.local_url)
        expected_upstream = [{"dial": f"{local.hostname}:{local.port}"}]
        for proxy in PROXIES:
            value = self._api("GET", f"/id/{proxy}")
            if (
                not isinstance(value, dict)
                or cast(dict[str, object], value).get("handler") != "reverse_proxy"
                or cast(dict[str, object], value).get("upstreams") != expected_upstream
            ):
                raise Failed("Switchstand Caddy proxy set is not exact")

    @staticmethod
    def _artifact_digest(path: Path, mode: int) -> str:
        descriptor = -1
        try:
            if not path.is_absolute() or path.resolve(strict=True) != path:
                raise OSError
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o777 != mode
            ):
                raise OSError
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()
        except (OSError, RuntimeError) as exc:
            raise Unknown(f"host artifact is not exact: {path.name}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _resume_trust(self) -> None:
        c = self.c
        try:
            attempt = c.attempt_dir.lstat()
            if (
                not c.attempt_dir.is_absolute() or c.attempt_dir.is_symlink()
                or not c.attempt_dir.is_dir() or attempt.st_uid != os.getuid()
                or attempt.st_mode & 0o777 != 0o700
                or c.attempt_dir.resolve(strict=True) != c.attempt_dir
            ):
                raise OSError
        except (OSError, RuntimeError) as exc:
            raise Unknown("attempt directory is not exact") from exc
        for runtime, expected in (
            (c.current_runtime, c.current_sha), (c.candidate_runtime, c.candidate_sha),
        ):
            if (
                not runtime.is_absolute() or runtime.is_symlink() or not runtime.is_dir()
                or runtime.resolve(strict=True) != runtime
            ):
                raise Unknown("runtime path is not exact")
            head = self._observe(
                ["git", "-C", str(runtime), "rev-parse", "HEAD"]
            ).stdout.strip()
            dirty = self._observe(
                ["git", "-C", str(runtime), "status", "--porcelain"]
            ).stdout
            if head != expected or dirty:
                raise Unknown("runtime checkout is not the exact clean SHA")
        if self._artifact_digest(c.candidate_launcher, 0o700) != c.candidate_launcher_sha:
            raise Unknown("candidate launcher changed")
        self._artifact_digest(c.env_file, 0o600)
        unit = self._observe([
            "systemctl", "--user", "show", c.service, "--property=ExecStart", "--value",
        ]).stdout
        if str(c.launcher) not in unit:
            raise Unknown("edge unit does not execute the selected launcher")

    @staticmethod
    def _observe(
        command: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        try:
            return run_host_command(command, check=check)
        except (OSError, subprocess.SubprocessError) as exc:
            raise Unknown("host state readback failed") from exc

    def _service_state(self) -> Literal["ACTIVE", "INACTIVE", "UNKNOWN"]:
        state = self._observe(
            ["systemctl", "--user", "is-active", self.c.service], check=False
        ).stdout.strip()
        if state == "active" and self._running_process_exact():
            return "ACTIVE"
        if state != "inactive":
            return "UNKNOWN"
        endpoint = urlparse(self.c.local_url)
        if endpoint.hostname is None or endpoint.port is None:
            return "UNKNOWN"
        try:
            with socket.create_connection(
                (endpoint.hostname, endpoint.port), timeout=1
            ):
                return "UNKNOWN"
        except OSError:
            return "INACTIVE"

    def reconcile_phase(
        self, phase: str, proof: dict[str, object], *, offline: bool
    ) -> tuple[str, dict[str, str]]:
        """Classify a durable host phase or its one proven next state without effects."""
        phases = (
            "PREFLIGHT", "GATED", "STOPPED", "SNAPSHOTTED", "OFFLINE_PENDING",
            "OFFLINE_PRE_MARKER", "OFFLINE_POSTGRES_AUTHORITY", "OFFLINE_COMPLETE",
            "SWAPPED", "STARTED", "UNGATED", "COMPLETE",
        )
        if phase not in phases:
            raise Unknown("host phase is unknown")
        self._resume_trust()
        try:
            gate = self._gate_state()
        except Failed as exc:
            raise Unknown("maintenance gate readback failed") from exc
        service = self._service_state()
        launcher = self._artifact_digest(self.c.launcher, 0o700)
        if phase in {"PREFLIGHT", "GATED", "STOPPED"} and (
            launcher != self.c.current_launcher_sha or os.path.lexists(self.backup)
        ):
            raise Unknown("launcher state does not match the durable phase")
        if phase == "PREFLIGHT":
            if os.path.lexists(self.snapshot_file):
                raise Unknown("preflight contains an unexpected snapshot")
            if gate == "ABSENT" and service == "ACTIVE":
                return phase, {}
            if gate == "APPLIED" and service == "ACTIVE" and self.public_gated():
                return "GATED", {}
            raise Unknown("gate outcome is ambiguous")
        if phase not in {"STARTED", "UNGATED", "COMPLETE"} and (
            gate != "APPLIED" or not self.public_gated()
        ):
            raise Unknown("exact maintenance gate is not proven")
        if phase == "GATED":
            if service == "ACTIVE":
                return phase, {}
            if service == "INACTIVE":
                return "STOPPED", {}
            raise Unknown("stop outcome is ambiguous")
        if phase not in {"SWAPPED", "STARTED", "UNGATED", "COMPLETE"} and service != "INACTIVE":
            raise Unknown("service is not proven stopped")
        snapshot = proof.get("fastmcp_snapshot")
        if phase == "STOPPED":
            if not os.path.lexists(self.snapshot_file):
                return phase, {}
            digest = self._artifact_digest(self.snapshot_file, 0o600)
            return "SNAPSHOTTED", {"fastmcp_snapshot": digest}
        if phase in phases[3:] and (
            not isinstance(snapshot, str)
            or snapshot != self._artifact_digest(self.snapshot_file, 0o600)
        ):
            raise Unknown("FastMCP snapshot does not match the durable proof")
        if phase in phases[3:8]:
            swapped = phase in {"SNAPSHOTTED", "OFFLINE_COMPLETE"} and not (
                phase == "SNAPSHOTTED" and offline
            )
            if swapped and launcher == self.c.candidate_launcher_sha and (
                self._artifact_digest(self.backup, 0o600) == self.c.current_launcher_sha
            ):
                return "SWAPPED", {}
            if launcher != self.c.current_launcher_sha or os.path.lexists(self.backup):
                raise Unknown("launcher state does not match the durable phase")
            return phase, {}
        if launcher != self.c.candidate_launcher_sha or (
            self._artifact_digest(self.backup, 0o600) != self.c.current_launcher_sha
        ):
            raise Unknown("candidate launcher state is not exact")
        if phase == "SWAPPED":
            if service == "INACTIVE":
                return phase, {}
            if service == "ACTIVE" and self.local_ready():
                return "STARTED", {}
            raise Unknown("start outcome is ambiguous")
        if service != "ACTIVE" or not self.local_ready():
            raise Unknown("candidate runtime is not exact")
        if phase == "STARTED" and gate == "ABSENT" and self.public_ready():
            return "UNGATED", {}
        if phase == "UNGATED" and gate == "APPLIED":
            return phase, {"gate_retained": "true"}
        if phase in {"UNGATED", "COMPLETE"} and gate == "ABSENT":
            return phase, {}
        if phase == "STARTED" and gate == "APPLIED":
            return phase, {}
        raise Unknown("ungate outcome is ambiguous")

    def gate(self) -> None:
        route = {
            "@id": GATE_ID,
            "match": [{"path": list(EDGE_PATHS)}],
            "handle": [
                {
                    "handler": "static_response",
                    "status_code": "503",
                    "headers": {"Retry-After": [str(self.c.retry_after)]},
                }
            ],
            "terminal": True,
        }
        try:
            self._api("PUT", "/config/apps/http/servers/dish_action_router/routes/0", route)
        except Interrupted:
            raise
        except Failed, Unknown:
            if not self.gate_exact():
                raise
        if not self.gate_exact():
            raise Unknown("maintenance gate readback mismatch")

    def gate_exact(self) -> bool:
        return self._gate_state() == "APPLIED"

    def _gate_state(self) -> Literal["ABSENT", "APPLIED", "UNKNOWN"]:
        value = self._api("GET", f"/id/{GATE_ID}")
        if value is None:
            return "ABSENT"
        if not isinstance(value, dict):
            return "UNKNOWN"
        route = cast(dict[str, object], value)
        exact = (
            route.get("terminal") is True
            and route.get("match") == [{"path": list(EDGE_PATHS)}]
            and route.get("handle")
            == [
                {
                    "handler": "static_response",
                    "status_code": "503",
                    "headers": {"Retry-After": [str(self.c.retry_after)]},
                }
            ]
        )
        return "APPLIED" if exact else "UNKNOWN"

    def public_gated(self) -> bool:
        try:
            parsed = urlparse(self.c.public_origin)
            port = parsed.port
        except ValueError:
            return False
        if parsed.scheme == "https" and parsed.hostname and port in (None, 443):
            probe = ExternalIngressHttp(
                self.c.public_origin + "/switchstand/mcp",
                self.c.public_origin + "/switchstand/mcp",
            )
            try:
                addresses = probe.public_addresses(parsed.hostname)
                for address in addresses:
                    for path in EDGE_PATHS:
                        probe_path = (
                            path[:-1] + "maintenance-probe" if path.endswith("/*") else path
                        )
                        status, headers, _ = probe.request(
                            parsed.hostname, address, "GET", probe_path
                        )
                        if status != 503 or headers.get("retry-after") != str(
                            self.c.retry_after
                        ):
                            return False
                return True
            except (OSError, ValueError, UnicodeError, http.client.HTTPException):
                return False
        for path in EDGE_PATHS:
            probe_path = path[:-1] + "maintenance-probe" if path.endswith("/*") else path
            target = self.c.public_origin + probe_path
            try:
                urllib.request.urlopen(target, timeout=5)
            except urllib.error.HTTPError as exc:
                if exc.code == 503 and exc.headers.get("Retry-After") == str(self.c.retry_after):
                    continue
            except OSError, TimeoutError:
                pass
            return False
        return True

    def _service(self, action: str, wanted: str) -> None:
        run_host_command(["systemctl", "--user", action, self.c.service], check=False)
        state = run_host_command(
            ["systemctl", "--user", "is-active", self.c.service], check=False
        ).stdout.strip()
        if (wanted == "active") != (state == "active"):
            raise Unknown(f"service did not become {wanted}")

    def _running_process_exact(self) -> bool:
        try:
            pid = int(
                run_host_command(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        self.c.service,
                        "--property=MainPID",
                        "--value",
                    ]
                ).stdout.strip()
            )
            process = Path(f"/proc/{pid}")
            arguments = process.joinpath("cmdline").read_bytes().split(b"\0")
            environment = dict(
                item.split(b"=", 1)
                for item in process.joinpath("environ").read_bytes().split(b"\0")
                if b"=" in item
            )
            configured = environment.get(b"FASTMCP_HOME")
            if configured is not None:
                fastmcp_home = Path(os.fsdecode(configured))
            else:
                data_home = environment.get(b"XDG_DATA_HOME")
                if data_home is not None:
                    fastmcp_home = Path(os.fsdecode(data_home)) / "fastmcp"
                else:
                    fastmcp_home = Path(os.fsdecode(environment[b"HOME"])) / ".local/share/fastmcp"
        except KeyError, OSError, UnicodeError, ValueError, subprocess.SubprocessError:
            return False
        return os.fsencode(self.c.launcher) in arguments and fastmcp_home == self.c.fastmcp_state

    def stop(self) -> None:
        self._service("stop", "inactive")
        endpoint = urlparse(self.c.local_url)
        try:
            with socket.create_connection(
                (cast(str, endpoint.hostname), cast(int, endpoint.port)), timeout=1
            ):
                pass
        except OSError:
            return
        raise Unknown("edge listener remains after service stop")

    def start(self) -> None:
        self._service("start", "active")
        restarts = run_host_command(
            [
                "systemctl",
                "--user",
                "show",
                self.c.service,
                "--property=NRestarts",
                "--value",
            ]
        ).stdout.strip()
        if restarts != "0":
            raise Unknown("edge service restarted during activation")

    def snapshot(self) -> None:
        temporary = self.snapshot_file.with_suffix(".tmp")
        try:
            run_host_command(
                [
                    "tar",
                    "-C",
                    str(self.c.fastmcp_state.parent),
                    "-cf",
                    str(temporary),
                    self.c.fastmcp_state.name,
                ]
            )
            os.chmod(temporary, 0o600)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, self.snapshot_file)
            directory = os.open(self.snapshot_file.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, subprocess.SubprocessError) as exc:
            temporary.unlink(missing_ok=True)
            raise Failed("FastMCP snapshot failed") from exc

    def snapshot_digest(self) -> str:
        return self._artifact_digest(self.snapshot_file, 0o600)

    def swap(self) -> None:
        atomic_copy(self.c.launcher, self.backup, 0o600)
        try:
            atomic_copy(self.c.candidate_launcher, self.c.launcher, 0o700)
        except OSError as exc:
            if _sha(self.c.launcher) != self.c.candidate_launcher_sha:
                raise Unknown("launcher replacement state is ambiguous") from exc
        if _sha(self.c.launcher) != self.c.candidate_launcher_sha:
            raise Unknown("candidate launcher readback mismatch")

    def _doctor(self, runtime: Path, expected_sha: str, public: bool) -> bool:
        command = [
            str(runtime / "scripts/switchstand-edge-doctor"),
            "--env-file",
            str(self.c.env_file),
            "--repo",
            str(runtime),
            "--expected-sha",
            expected_sha,
        ]
        if not public:
            command += ["--local-url", self.c.local_url, "--public-url", self.c.local_url]
        return run_host_command(command, check=False).returncode == 0

    def local_ready(self) -> bool:
        return (
            self._doctor(self.c.candidate_runtime, self.c.candidate_sha, False)
            and (_sha(self.c.launcher) == self.c.candidate_launcher_sha)
            and self._running_process_exact()
        )

    def rollback_ready(self) -> bool:
        return (
            self._doctor(self.c.current_runtime, self.c.current_sha, False)
            and (_sha(self.c.launcher) == self.c.current_launcher_sha)
            and self._running_process_exact()
        )

    def ungate(self) -> None:
        try:
            self._api("DELETE", f"/id/{GATE_ID}")
        except Interrupted:
            raise
        except Failed, Unknown:
            if self.gate_exact():
                raise
        if self.gate_exact():
            raise Unknown("maintenance gate removal is ambiguous")

    def public_ready(self) -> bool:
        candidate = _sha(self.c.launcher) == self.c.candidate_launcher_sha
        runtime = self.c.candidate_runtime if candidate else self.c.current_runtime
        expected = self.c.candidate_sha if candidate else self.c.current_sha
        public_url = self.c.public_origin + "/switchstand/mcp"
        ingress = ExternalIngressHttp(public_url, public_url).observe()
        return (
            ingress.transport_ok
            and ingress.valid_auth_challenge
            and self._doctor(runtime, expected, True)
        )

    def restore_launcher(self) -> None:
        if _sha(self.c.launcher) != self.c.current_launcher_sha:
            atomic_copy(self.backup, self.c.launcher, 0o700)


class Receipt:
    def __init__(self, config: Config, offline: OfflineStep | None):
        self.path = config.attempt_dir / "receipt.json"
        expected: dict[str, object] = {
            "schema": 1,
            "target": config.target,
            "target_root": str(config.target_root) if config.target_root else None,
            "service": config.service,
            "fastmcp_state": str(config.fastmcp_state),
            "lock_path": str(config.lock_path),
            "caddy": config.caddy,
            "local_url": config.local_url,
            "public_origin": config.public_origin,
            "current_sha": config.current_sha,
            "candidate_sha": config.candidate_sha,
            "current_runtime": str(config.current_runtime),
            "candidate_runtime": str(config.candidate_runtime),
            "candidate_launcher": str(config.candidate_launcher),
            "launcher": str(config.launcher),
            "env_file": str(config.env_file),
            "current_launcher_sha": config.current_launcher_sha,
            "candidate_launcher_sha": config.candidate_launcher_sha,
            "retry_after": config.retry_after,
            "offline_receipt": str(offline.receipt_path) if offline else None,
            "offline_candidate": offline.candidate_sha if offline else None,
            "offline_corpus": list(offline.corpus_manifests) if offline else None,
            "offline_worksheet": offline.worksheet if offline else None,
            "authority_crossed": False,
            "status": "RUNNING",
            "phase": "PREFLIGHT",
        }
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            self.existing, self.value = False, expected
            return
        except OSError as exc:
            raise Unknown("host receipt is unreadable") from exc
        self.existing = True
        try:
            metadata = os.fstat(descriptor)
            with os.fdopen(descriptor) as handle:
                loaded = json.load(handle)
        except (OSError, UnicodeError, ValueError) as exc:
            raise Unknown("host receipt is unreadable") from exc
        if not isinstance(loaded, dict):
            raise Unknown("host receipt is malformed")
        value = cast(dict[str, object], loaded)
        phases = {
            "PREFLIGHT", "GATED", "STOPPED", "SNAPSHOTTED", "OFFLINE_PENDING",
            *OFFLINE_PHASES.values(), "SWAPPED", "STARTED", "UNGATED", "COMPLETE",
            "ROLLED_BACK",
        }
        fixed = {"status", "phase", "authority_crossed", "error", "fastmcp_snapshot"}
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o777 != 0o600
            or any(value.get(key) != item for key, item in expected.items() if key not in fixed)
            or value.get("status") not in {"RUNNING", "UNKNOWN", "PASS", "FAIL"}
            or value.get("phase") not in phases
            or not isinstance(value.get("authority_crossed"), bool)
            or not set(value).issubset(set(expected) | {"error", "fastmcp_snapshot"})
            or ("error" in value and not isinstance(value["error"], str))
        ):
            raise Unknown("host receipt is malformed or belongs to another attempt")
        phase, status = value["phase"], value["status"]
        crossed = cast(bool, value["authority_crossed"])
        post_authority = phase in {
            "OFFLINE_POSTGRES_AUTHORITY", "OFFLINE_COMPLETE", "SWAPPED", "STARTED",
            "UNGATED", "COMPLETE",
        }
        if (status == "PASS") != (phase == "COMPLETE") or (
            status == "FAIL"
        ) != (phase == "ROLLED_BACK") or crossed != (offline is not None and post_authority):
            raise Unknown("host receipt terminal state is inconsistent")
        if phase not in {"PREFLIGHT", "GATED", "STOPPED"} and not re.fullmatch(
            r"[0-9a-f]{64}", cast(str, value.get("fastmcp_snapshot", ""))
        ):
            raise Unknown("host receipt phase proof is incomplete")
        self.value = value

    def write(self, phase: str, status: str = "RUNNING", error: str | None = None) -> None:
        self.value.update(phase=phase, status=status)
        if error:
            self.value["error"] = error
        else:
            self.value.pop("error", None)
        payload = (json.dumps(self.value, sort_keys=True) + "\n").encode()
        atomic_replace_bytes(self.path, payload)


def retain_gate(operations: Operations) -> None:
    """Retain and verify the public maintenance gate or report UNKNOWN."""
    try:
        if not operations.gate_exact():
            operations.gate()
        if not operations.gate_exact() or not operations.public_gated():
            raise Unknown("gate is not exact")
    except (Failed, Unknown, OSError, subprocess.SubprocessError) as exc:
        raise GateRetentionUnknown("maintenance gate retention is unknown") from exc


def deploy(config: Config, operations: Operations, offline: OfflineStep | None = None) -> str:
    try:
        validate_target(config)
    except (Failed, OSError):
        return "FAIL"
    try:
        receipt = Receipt(config, offline)
    except Unknown:
        try:
            retain_gate(operations)
        except GateRetentionUnknown:
            pass
        return "UNKNOWN"
    if receipt.value["status"] in {"PASS", "FAIL"}:
        return cast(str, receipt.value["status"])
    phase = cast(str, receipt.value["phase"])
    authority_crossed = cast(bool, receipt.value["authority_crossed"])
    gate_retained = False
    gate_attempted = phase != "PREFLIGHT"
    try:
        if receipt.existing:
            offline_started = phase in {
                "OFFLINE_PENDING", *OFFLINE_PHASES.values(), "SWAPPED", "STARTED",
                "UNGATED", "COMPLETE",
            }
            if offline is not None and offline_started:
                if offline.receipt_path.exists():
                    boundary = _offline_boundary(offline, config)
                    observed = OFFLINE_PHASES[boundary]
                    ordered = list(OFFLINE_PHASES.values())
                    if phase in ordered and ordered.index(observed) < ordered.index(phase):
                        raise Unknown("offline receipt trails the host receipt")
                    if phase in {"SWAPPED", "STARTED", "UNGATED", "COMPLETE"} and (
                        boundary != "COMPLETE"
                    ):
                        raise Unknown("offline receipt does not prove host authority phase")
                    if boundary in {"POSTGRES_AUTHORITY", "COMPLETE"}:
                        authority_crossed = True
                    if phase == "OFFLINE_PENDING" or (
                        phase in ordered and ordered.index(observed) > ordered.index(phase)
                    ):
                        phase = observed
                        receipt.value["authority_crossed"] = authority_crossed
                        receipt.write(phase)
                elif phase != "OFFLINE_PENDING":
                    raise Unknown("offline receipt is missing")
            observed, proof = operations.reconcile_phase(
                phase, receipt.value, offline=offline is not None
            )
            gate_retained = proof.pop("gate_retained", None) == "true"
            if observed != phase or proof:
                phase = observed
                receipt.value.update(proof)
                receipt.write(phase)
        else:
            receipt.write(phase)
            operations.preflight()
        if phase == "PREFLIGHT":
            gate_attempted = True
            operations.gate()
            phase = "GATED"
            receipt.write(phase)
        if phase == "GATED":
            if not operations.public_gated():
                raise Unknown("public maintenance gate is not exact")
            operations.stop()
            phase = "STOPPED"
            receipt.write(phase)
        if phase == "STOPPED":
            operations.snapshot()
            receipt.value["fastmcp_snapshot"] = operations.snapshot_digest()
            phase = "SNAPSHOTTED"
            receipt.write(phase)
        if offline is not None and phase in {"SNAPSHOTTED", "OFFLINE_PENDING", *OFFLINE_PHASES.values()}:
            if phase == "SNAPSHOTTED":
                phase = "OFFLINE_PENDING"
                receipt.write(phase)
            expected = iter(OFFLINE_PHASES)
            advanced: list[str] = []

            def advance(boundary: str) -> None:
                nonlocal phase, authority_crossed
                observed = _offline_boundary(offline, config)
                if (
                    boundary != next(expected, None)
                    or list(OFFLINE_PHASES).index(observed)
                    < list(OFFLINE_PHASES).index(boundary)
                ):
                    raise Unknown("offline phase or receipt advanced out of order")
                advanced.append(boundary)
                candidate = OFFLINE_PHASES[boundary]
                if boundary in {"POSTGRES_AUTHORITY", "COMPLETE"}:
                    authority_crossed = True
                    receipt.value["authority_crossed"] = True
                phases = ["OFFLINE_PENDING", *OFFLINE_PHASES.values()]
                if phases.index(candidate) >= phases.index(phase):
                    phase = candidate
                    receipt.write(phase)

            offline.run(advance)
            if advanced != list(OFFLINE_PHASES):
                raise Unknown("offline step did not reach its terminal boundary")
            phase = "OFFLINE_COMPLETE"
        if phase in {"SNAPSHOTTED", "OFFLINE_COMPLETE"}:
            operations.swap()
            phase = "SWAPPED"
            receipt.write(phase)
        if phase == "SWAPPED":
            operations.start()
            phase = "STARTED"
            receipt.write(phase)
        if not operations.local_ready():
            raise Failed("candidate local verification failed")
        if phase == "STARTED" or gate_retained:
            operations.ungate()
            phase = "UNGATED"
            receipt.write(phase)
        if not operations.public_ready():
            raise Unknown("candidate public verification failed after ungating")
        receipt.write("COMPLETE", "PASS")
        return "PASS"
    except Unknown as exc:
        error = type(exc).__name__
        try:
            if gate_attempted:
                retain_gate(operations)
        except GateRetentionUnknown:
            error = "GateRetentionUnknown"
        receipt.write(phase, "UNKNOWN", error)
        return "UNKNOWN"
    except (Failed, OSError, subprocess.SubprocessError) as exc:
        if (
            phase == "PREFLIGHT" and not receipt.existing
            and offline is not None and offline.receipt_path.exists()
        ):
            try:
                retain_gate(operations)
            except GateRetentionUnknown:
                receipt.write(phase, "UNKNOWN", "GateRetentionUnknown")
                return "UNKNOWN"
            receipt.write(phase, "UNKNOWN", "OfflinePreflightUnknown")
            return "UNKNOWN"
        if authority_crossed:
            try:
                retain_gate(operations)
            except GateRetentionUnknown:
                receipt.write(phase, "UNKNOWN", "GateRetentionUnknown")
                return "UNKNOWN"
            receipt.write(phase, "UNKNOWN", "ForwardFixRequired")
            return "UNKNOWN"
        try:
            if phase in {"OFFLINE_PENDING", "OFFLINE_PRE_MARKER"}:
                if offline is None or (
                    offline.receipt_path.exists()
                    and _offline_boundary(offline, config) != "PRE_MARKER"
                ):
                    raise Unknown("pre-authority boundary is not proven")
                offline.abort_pre_authority()
                operations.start()
                if not operations.rollback_ready():
                    raise Unknown("pre-authority rollback verification failed")
            elif phase in {"SWAPPED", "STARTED", "UNGATED"}:
                operations.stop()
                operations.restore_launcher()
                operations.start()
                if not operations.rollback_ready():
                    raise Unknown("rollback local verification failed")
            elif phase in {"STOPPED", "SNAPSHOTTED"}:
                operations.start()
            if phase != "PREFLIGHT":
                operations.ungate()
            if phase != "PREFLIGHT" and not operations.public_ready():
                raise Unknown("rollback public verification failed")
        except Failed, Unknown, OSError, subprocess.SubprocessError:
            error = "RollbackUnknown"
            try:
                retain_gate(operations)
            except GateRetentionUnknown:
                error = "GateRetentionUnknown"
            receipt.write(phase, "UNKNOWN", error)
            return "UNKNOWN"
        receipt.write("ROLLED_BACK", "FAIL", type(exc).__name__)
        return "FAIL"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "attempt-dir",
        "current-runtime",
        "candidate-runtime",
        "candidate-launcher",
        "launcher",
        "fastmcp-state",
        "env-file",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in (
        "current-sha",
        "candidate-sha",
        "candidate-launcher-sha",
        "current-launcher-sha",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--retry-after", type=int, default=60)
    args = parser.parse_args(argv)
    if (
        not re.fullmatch(r"[0-9a-f]{40}", args.current_sha)
        or not re.fullmatch(r"[0-9a-f]{40}", args.candidate_sha)
        or args.retry_after < 1
    ):
        parser.error("candidate SHA or retry interval is invalid")
    config = Config(**vars(args), target="production")
    validate_target(config)
    args.attempt_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    with exclusive_lock(config.lock_path):

        def interrupted(signum: int, _frame: object) -> None:
            raise Interrupted(f"maintenance interrupted by signal {signum}")

        previous_int = signal.signal(signal.SIGINT, interrupted)
        previous_term = signal.signal(signal.SIGTERM, interrupted)
        try:
            result = deploy(config, HostOperations(config))
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
    print(result)
    return {"PASS": 0, "FAIL": 1, "UNKNOWN": 2}[result]


if __name__ == "__main__":
    raise SystemExit(main())
