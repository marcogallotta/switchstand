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
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO, cast
from urllib.parse import urlparse

from .edge_monitor_host import ExternalIngressHttp

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


class Failed(RuntimeError):
    """A definite failure which can be rolled back."""


class Unknown(RuntimeError):
    """An effect whose state cannot be established."""


class Interrupted(Unknown):
    """An operator interrupt which mutation reconciliation must not suppress."""


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
    retry_after: int = 60
    caddy: str = CADDY
    public_origin: str = PUBLIC_ORIGIN


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


def _sha(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, capture_output=True, text=True, timeout=45)


def _atomic_copy(source: Path, target: Path, mode: int) -> None:
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
    if config.fastmcp_state != FASTMCP_STATE:
        raise Failed("FastMCP state path is not the production edge store")
    current = config.launcher.read_bytes()
    old_runtime = os.fsencode(config.current_runtime)
    if current.count(old_runtime) != 1:
        raise Failed("current launcher does not bind exactly one runtime")
    expected = current.replace(old_runtime, os.fsencode(config.candidate_runtime), 1)
    if config.candidate_launcher.read_bytes() != expected:
        raise Failed("candidate launcher is not the exact runtime retarget")


def _exclusive_lock(path: Path = LOCK) -> TextIO:
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
            head = _run(["git", "-C", str(runtime), "rev-parse", "HEAD"]).stdout.strip()
            dirty = _run(["git", "-C", str(runtime), "status", "--porcelain"]).stdout
            if head != expected or dirty:
                raise Failed("runtime checkout is not the exact clean SHA")
            if '"fastmcp-slim[server]==4.0.3"' not in (runtime / "pyproject.toml").read_text():
                raise Failed("runtime does not pin the approved FastMCP version")
        unit = _run(
            [
                "systemctl",
                "--user",
                "show",
                SERVICE,
                "--property=ExecStart",
                "--value",
            ]
        ).stdout
        if str(c.launcher) not in unit:
            raise Failed("edge unit does not execute the selected launcher")
        if (
            _run(["systemctl", "--user", "is-active", SERVICE], check=False).stdout.strip()
            != "active"
        ):
            raise Failed("edge service is not active")
        if not self._running_process_exact():
            raise Failed("running edge launcher or FastMCP state is not exact")
        if self.gate_exact():
            raise Failed("maintenance gate already exists")
        for proxy in PROXIES:
            value = self._api("GET", f"/id/{proxy}")
            if (
                not isinstance(value, dict)
                or cast(dict[str, object], value).get("handler") != "reverse_proxy"
                or cast(dict[str, object], value).get("upstreams") != [{"dial": "127.0.0.1:8790"}]
            ):
                raise Failed("Switchstand Caddy proxy set is not exact")

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
        value = self._api("GET", f"/id/{GATE_ID}")
        if not isinstance(value, dict):
            return False
        route = cast(dict[str, object], value)
        return (
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
        _run(["systemctl", "--user", action, SERVICE], check=False)
        state = _run(["systemctl", "--user", "is-active", SERVICE], check=False).stdout.strip()
        if (wanted == "active") != (state == "active"):
            raise Unknown(f"service did not become {wanted}")

    def _running_process_exact(self) -> bool:
        try:
            pid = int(
                _run(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        SERVICE,
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
        try:
            with socket.create_connection(("127.0.0.1", 8790), timeout=1):
                pass
        except OSError:
            return
        raise Unknown("edge listener remains after service stop")

    def start(self) -> None:
        self._service("start", "active")
        restarts = _run(
            [
                "systemctl",
                "--user",
                "show",
                SERVICE,
                "--property=NRestarts",
                "--value",
            ]
        ).stdout.strip()
        if restarts != "0":
            raise Unknown("edge service restarted during activation")

    def snapshot(self) -> None:
        temporary = self.snapshot_file.with_suffix(".tmp")
        try:
            _run(
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

    def swap(self) -> None:
        _atomic_copy(self.c.launcher, self.backup, 0o600)
        try:
            _atomic_copy(self.c.candidate_launcher, self.c.launcher, 0o700)
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
            command += ["--local-url", LOCAL_URL, "--public-url", LOCAL_URL]
        return _run(command, check=False).returncode == 0

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
            _atomic_copy(self.backup, self.c.launcher, 0o700)


class Receipt:
    def __init__(self, config: Config):
        self.path = config.attempt_dir / "receipt.json"
        self.value: dict[str, object] = {
            "schema": 1,
            "current_sha": config.current_sha,
            "candidate_sha": config.candidate_sha,
            "current_launcher_sha": config.current_launcher_sha,
            "candidate_launcher_sha": config.candidate_launcher_sha,
            "status": "RUNNING",
            "phase": "PREFLIGHT",
        }

    def write(self, phase: str, status: str = "RUNNING", error: str | None = None) -> None:
        self.value.update(phase=phase, status=status)
        if error:
            self.value["error"] = error
        payload = (json.dumps(self.value, sort_keys=True) + "\n").encode()
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def deploy(config: Config, operations: Operations) -> str:
    receipt = Receipt(config)
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
        operations.stop()
        phase = "STOPPED"
        receipt.write(phase)
        operations.snapshot()
        phase = "SNAPSHOTTED"
        receipt.write(phase)
        operations.swap()
        phase = "SWAPPED"
        receipt.write(phase)
        operations.start()
        phase = "STARTED"
        receipt.write(phase)
        if not operations.local_ready():
            raise Failed("candidate local verification failed")
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
            if gate_attempted and not operations.gate_exact():
                operations.gate()
        except Failed, Unknown, OSError, subprocess.SubprocessError:
            error = "GateRetentionUnknown"
        receipt.write(phase, "UNKNOWN", error)
        return "UNKNOWN"
    except (Failed, OSError, subprocess.SubprocessError) as exc:
        try:
            if phase in {"SWAPPED", "STARTED", "UNGATED"}:
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
                if not operations.gate_exact():
                    operations.gate()
            except Failed, Unknown, OSError, subprocess.SubprocessError:
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
    args.attempt_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    config = Config(**vars(args))
    with _exclusive_lock():

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
