"""One-shot host and fixture runner for the inert Wakeful edge monitor."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
from uuid import UUID

import httpx

from .edge_monitor import (
    CanaryTarget,
    EdgeMonitor,
    FunctionalObservation,
    FunctionalProbe,
    FunctionalStatus,
    HttpObservation,
    HttpProbe,
    JournalBatch,
    JournalProbe,
    SystemdObservation,
    SystemdProbe,
)
from .wakeful import WakefulStore

PROTOCOL = "2025-03-26"


def _run(argv: list[str]) -> str:
    return subprocess.run(
        argv, check=True, capture_output=True, text=True, timeout=10, shell=False
    ).stdout


@dataclass(frozen=True)
class HostSystemd:
    service: str

    def observe(self) -> SystemdObservation:
        try:
            values = _run([
                "systemctl", "--user", "show", self.service,
                "--property=ActiveState", "--property=MainPID",
            ]).splitlines()
            fields = dict(line.split("=", 1) for line in values)
            return SystemdObservation(fields.get("ActiveState") == "active", int(fields["MainPID"]))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return SystemdObservation(False, 0)


@dataclass(frozen=True)
class HostJournal:
    service: str

    def read_after(self, cursor: str | None) -> JournalBatch:
        argv = ["journalctl", "--user", "--unit", self.service, "--show-cursor"]
        argv += ["--after-cursor", cursor, "--output=json"] if cursor else ["--lines=0"]
        output = _run(argv)
        next_cursor = cursor
        messages: list[str] = []
        for line in output.splitlines():
            if line.startswith("-- cursor: "):
                next_cursor = line.removeprefix("-- cursor: ").strip()
            elif line.startswith("{"):
                record = json.loads(line)
                next_cursor = str(record.get("__CURSOR", next_cursor or ""))
                messages.append(str(record.get("MESSAGE", "")))
        if not next_cursor:
            raise RuntimeError("journalctl did not return a cursor")
        return JournalBatch(next_cursor, tuple(messages))


@dataclass(frozen=True)
class HostHttp:
    urls: tuple[str, ...]
    resource: str

    def observe(self) -> HttpObservation:
        resource_url = httpx.URL(self.resource)
        if resource_url.scheme != "https" or not resource_url.host or resource_url.userinfo:
            return HttpObservation(True, 500)
        expected_metadata = str(
            resource_url.copy_with(
                path="/.well-known/oauth-protected-resource" + urlparse(self.resource).path
            )
        )
        try:
            with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
                for raw in self.urls:
                    target = httpx.URL(raw)
                    if target.scheme not in ("http", "https") or not target.host or target.userinfo:
                        return HttpObservation(True, 500)
                    metadata_url = target.copy_with(
                        path="/.well-known/oauth-protected-resource" + target.path
                    )
                    challenge = client.post(target)
                    metadata = client.get(metadata_url)
                    body_value = cast(object, metadata.json())
                    body = cast(dict[str, object], body_value) if isinstance(body_value, dict) else {}
                    valid = (
                        challenge.status_code == 401
                        and f'resource_metadata="{expected_metadata}"'
                        in challenge.headers.get("www-authenticate", "")
                        and metadata.status_code == 200
                        and body.get("resource") == self.resource
                    )
                    if not valid:
                        return HttpObservation(True, 500)
        except (OSError, ValueError, httpx.HTTPError, json.JSONDecodeError):
            return HttpObservation(False, None)
        return HttpObservation(True, 401, valid_auth_challenge=True)


@dataclass(frozen=True)
class HostFunctional:
    url: str
    token: str

    def observe(self, target: CanaryTarget) -> FunctionalObservation:
        headers = {
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {self.token}",
            "content-type": "application/json",
        }
        try:
            with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                initialized = client.post(self.url, headers=headers, json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "clientInfo": {"name": "switchstand-wakeful", "version": "1"},
                        "protocolVersion": PROTOCOL, "capabilities": {},
                    },
                })
                session = initialized.headers["mcp-session-id"]
                session_headers = headers | {
                    "mcp-session-id": session, "mcp-protocol-version": PROTOCOL,
                }
                ready = client.post(self.url, headers=session_headers, json={
                    "jsonrpc": "2.0", "method": "notifications/initialized",
                })
                response = client.post(self.url, headers=session_headers, json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                        "name": "work_get",
                        "arguments": {"api_version": "1", "work_id": target.work_id},
                    },
                })
                initialized.raise_for_status()
                ready.raise_for_status()
                response.raise_for_status()
                result_value = cast(object, response.json()["result"]["structuredContent"])
        except (OSError, KeyError, TypeError, ValueError, httpx.HTTPError):
            return FunctionalObservation(FunctionalStatus.FAILED)
        if not isinstance(result_value, dict):
            return FunctionalObservation(FunctionalStatus.FAILED)
        result = cast(dict[str, object], result_value)
        status = result.get("status")
        item_value = result.get("item")
        item = cast(dict[str, object], item_value) if isinstance(item_value, dict) else {}
        if status == "ok":
            return FunctionalObservation(
                FunctionalStatus.OK
                if str(item.get("id", "")) == target.work_id
                else FunctionalStatus.FAILED
            )
        if status == "provider_error":
            return FunctionalObservation(FunctionalStatus.PROVIDER_ERROR)
        return FunctionalObservation(FunctionalStatus.FAILED)


@dataclass(frozen=True)
class FixedProbe:
    value: Any

    def observe(self, *_args: object) -> Any:
        return self.value


@dataclass(frozen=True)
class FixedFunctional:
    value: FunctionalObservation

    def observe(self, target: CanaryTarget) -> FunctionalObservation:
        return self.value


@dataclass(frozen=True)
class FixedJournal:
    message: str

    def read_after(self, cursor: str | None) -> JournalBatch:
        return JournalBatch(cursor or "fixture-cursor", (self.message,) if self.message else ())


def _private_state(path: Path | None) -> Path:
    if path is None:
        root = Path.home() / ".local/state/switchstand/wakeful-runs"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = Path(tempfile.mkdtemp(prefix="qualification-", dir=root))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _canary(token_path: Path | None, work_id: str | None) -> tuple[CanaryTarget | None, str]:
    if token_path is None or work_id is None:
        return None, ""
    try:
        if token_path.stat().st_mode & 0o7777 != 0o600:
            return None, ""
        normalized_work_id = str(UUID(work_id))
        token = token_path.read_text().strip()
    except (OSError, UnicodeError, ValueError):
        return None, ""
    return (
        (CanaryTarget("fixed-read-only-bearer", normalized_work_id), token)
        if token else (None, "")
    )


def _functional_target(
    local_url: str | None, public_url: str | None, resource_url: str | None,
) -> str | None:
    try:
        resource = httpx.URL(resource_url or "")
        selected = httpx.URL(public_url or local_url or "")
        if resource.scheme != "https" or not resource.host or resource.userinfo:
            return None
        if public_url:
            return str(selected) if selected == resource else None
        host = selected.host
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except (ValueError, UnicodeError):
        return None
    valid_local = (
        selected.scheme == "http" and loopback and not selected.userinfo
        and selected.path == "/mcp" and not selected.query and not selected.fragment
    )
    return str(selected) if valid_local else None


def _configured_canary(
    token_path: Path | None, work_id: str | None, local_url: str | None,
    public_url: str | None, resource_url: str | None,
) -> tuple[CanaryTarget | None, str, str]:
    target_url = _functional_target(local_url, public_url, resource_url)
    if target_url is None:
        return None, "", ""
    canary, token = _canary(token_path, work_id)
    return canary, token, target_url


def _fixture(
    name: str,
) -> tuple[SystemdProbe, JournalProbe, HttpProbe, FunctionalProbe, CanaryTarget]:
    message = "bad_refresh_token" if name == "bad_refresh_token" else ""
    return (
        FixedProbe(SystemdObservation(True, 1234)),
        FixedJournal(message),
        FixedProbe(HttpObservation(True, 200)),
        FixedFunctional(FunctionalObservation(FunctionalStatus.OK)),
        CanaryTarget("fixture", "00000000-0000-0000-0000-000000000001"),
    )


def _check(args: argparse.Namespace, state: Path) -> dict[str, object]:
    if args.fixture:
        systemd, journal, http, functional, canary = _fixture(args.fixture)
    else:
        canary, token, functional_url = _configured_canary(
            args.bearer_token_file, args.work_id, args.local_url,
            args.public_url, args.resource_url,
        )
        systemd: SystemdProbe = HostSystemd(args.service)
        journal: JournalProbe = HostJournal(args.service)
        http: HttpProbe = HostHttp(
            tuple(filter(None, (args.local_url, args.public_url))), args.resource_url
        )
        functional: FunctionalProbe = HostFunctional(functional_url, token)
    store = WakefulStore(state / "wakeful.sqlite3")
    result = EdgeMonitor(
        monitor_id="chatgpt-edge", subject=args.service, store=store,
        systemd=systemd, journal=journal, http=http, functional=functional, canary=canary,
    ).run_once()
    return {
        "condition": result.condition.value if result.condition else None,
        "emitted": result.emitted,
        "lease_busy": result.lease_busy,
        "pending": len(store.pending()),
        "state_dir": str(state),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--state-dir", type=Path)
    sub = result.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--fixture", choices=("healthy", "bad_refresh_token"))
    check.add_argument("--service", default="switchstand-chatgpt-mcp.service")
    check.add_argument("--local-url")
    check.add_argument("--public-url")
    check.add_argument("--resource-url")
    check.add_argument("--bearer-token-file", type=Path)
    check.add_argument("--work-id")
    sub.add_parser("events")
    sub.add_parser("demo")
    return result


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if args.command == "check" and not args.fixture and (
        not args.local_url or not args.resource_url
    ):
        argument_parser.error("real check requires --local-url and --resource-url")
    state = _private_state(args.state_dir)
    if args.command == "events":
        print(json.dumps([asdict(event) for event in WakefulStore(state / "wakeful.sqlite3").pending()]))
    elif args.command == "demo":
        outputs: list[dict[str, object]] = []
        for fixture in ("healthy", "bad_refresh_token", "bad_refresh_token", "healthy", "healthy"):
            check_args = parser().parse_args(["check", "--fixture", fixture])
            outputs.append(_check(check_args, state))
        print(json.dumps({"cycles": outputs, "events": [
            asdict(event) for event in WakefulStore(state / "wakeful.sqlite3").pending()
        ]}))
    else:
        print(json.dumps(_check(args, state)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
