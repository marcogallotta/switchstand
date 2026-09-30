"""One-shot host and fixture runner for the inert Wakeful edge monitor."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import socket
import ssl
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
PUBLIC_DNS_URL = "https://cloudflare-dns.com/dns-query"
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")


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


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Use a public-DNS address while preserving the origin Host and TLS SNI."""

    def __init__(self, host: str, address: str):
        self.ssl_context = ssl.create_default_context()
        super().__init__(host, port=443, timeout=3, context=self.ssl_context)
        self.address = address

    def connect(self) -> None:
        raw = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self.ssl_context.wrap_socket(raw, server_hostname=self.host)


@dataclass(frozen=True)
class ExternalIngressHttp:
    """Probe the public Funnel path without split-DNS tailnet short-circuiting."""

    url: str
    resource: str
    resolver_url: str = PUBLIC_DNS_URL

    def _addresses(self, host: str) -> tuple[str, ...]:
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
            response = client.get(
                self.resolver_url,
                params={"name": host, "type": "A"},
                headers={"accept": "application/dns-json"},
            )
            response.raise_for_status()
            payload_value = cast(object, response.json())
        payload = cast(dict[str, object], payload_value) if isinstance(payload_value, dict) else {}
        answers_value = payload.get("Answer", [])
        answers = cast(list[object], answers_value) if isinstance(answers_value, list) else []
        addresses: list[str] = []
        for answer_value in answers:
            if not isinstance(answer_value, dict):
                continue
            answer = cast(dict[str, object], answer_value)
            if answer.get("type") != 1:
                continue
            try:
                address = ipaddress.ip_address(str(answer.get("data", "")))
            except ValueError:
                continue
            if address.is_global and address not in TAILSCALE_CGNAT:
                addresses.append(str(address))
        if not addresses:
            raise ValueError("public DNS returned no global IPv4 address")
        return tuple(dict.fromkeys(addresses))

    @staticmethod
    def _request(host: str, address: str, method: str, path: str) -> tuple[int, str, bytes]:
        connection = _PinnedHTTPSConnection(host, address)
        try:
            connection.request(method, path, headers={"accept": "application/json"})
            response = connection.getresponse()
            return response.status, response.getheader("www-authenticate", ""), response.read()
        finally:
            connection.close()

    def observe(self) -> HttpObservation:
        try:
            target = httpx.URL(self.url)
            resource = httpx.URL(self.resource)
            if (
                target != resource
                or target.scheme != "https"
                or not target.host
                or target.port not in (None, 443)
                or target.userinfo
                or target.query
                or target.fragment
            ):
                return HttpObservation(True, 500)
            metadata_path = "/.well-known/oauth-protected-resource" + target.path
            expected_metadata = str(target.copy_with(path=metadata_path))
            for address in self._addresses(target.host):
                challenge, authenticate, _ = self._request(
                    target.host, address, "POST", target.raw_path.decode()
                )
                metadata, _, body_raw = self._request(
                    target.host, address, "GET", metadata_path
                )
                body_value = cast(object, json.loads(body_raw))
                body = cast(dict[str, object], body_value) if isinstance(body_value, dict) else {}
                if not (
                    challenge == 401
                    and f'resource_metadata="{expected_metadata}"' in authenticate
                    and metadata == 200
                    and body.get("resource") == self.resource
                ):
                    return HttpObservation(True, 500)
        except (
            OSError, ValueError, UnicodeError, http.client.HTTPException,
            httpx.HTTPError, json.JSONDecodeError,
        ):
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


def _external_ingress(
    public_url: str | None, resource_url: str | None, resolver_url: str,
) -> HttpProbe | None:
    try:
        if not public_url or httpx.URL(public_url) != httpx.URL(resource_url or ""):
            return None
    except (ValueError, UnicodeError):
        return None
    return ExternalIngressHttp(public_url, resource_url or "", resolver_url)


def _fixture(
    name: str,
) -> tuple[SystemdProbe, JournalProbe, HttpProbe, HttpProbe | None, FunctionalProbe, CanaryTarget]:
    message = "bad_refresh_token" if name == "bad_refresh_token" else ""
    return (
        FixedProbe(SystemdObservation(True, 1234)),
        FixedJournal(message),
        FixedProbe(HttpObservation(True, 200)),
        None,
        FixedFunctional(FunctionalObservation(FunctionalStatus.OK)),
        CanaryTarget("fixture", "00000000-0000-0000-0000-000000000001"),
    )


def _check(args: argparse.Namespace, state: Path) -> dict[str, object]:
    if args.fixture:
        systemd, journal, http, ingress, functional, canary = _fixture(args.fixture)
    else:
        canary, token, functional_url = _configured_canary(
            args.bearer_token_file, args.work_id, args.local_url,
            args.public_url, args.resource_url,
        )
        systemd: SystemdProbe = HostSystemd(args.service)
        journal: JournalProbe = HostJournal(args.service)
        http: HttpProbe = HostHttp(
            (args.local_url,), args.resource_url
        )
        ingress = _external_ingress(
            args.public_url, args.resource_url, args.public_dns_url
        )
        functional: FunctionalProbe = HostFunctional(functional_url, token)
    store = WakefulStore(state / "wakeful.sqlite3")
    result = EdgeMonitor(
        monitor_id="chatgpt-edge", subject=args.service, store=store,
        systemd=systemd, journal=journal, http=http, functional=functional, canary=canary,
        ingress=ingress,
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
    check.add_argument("--public-dns-url", default=PUBLIC_DNS_URL)
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
