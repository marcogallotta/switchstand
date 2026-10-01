"""Authenticated post-start MCP semantics probe with inert defaults."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import cast
from urllib.parse import urlparse
from uuid import UUID

import httpx

PROTOCOL = "2025-03-26"


class ProbeFailure(RuntimeError):
    pass


def _private(path: Path) -> str:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o777 != 0o600:
            raise ProbeFailure(f"{path.name} must be a mode-0600 regular file")
        with os.fdopen(descriptor, closefd=False) as stream:
            return stream.read().strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _digest(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


class MCP:
    def __init__(self, endpoint: str, token: str):
        self.endpoint, self.token = endpoint, token
        self.client = httpx.Client(timeout=10, trust_env=False, follow_redirects=False)
        self.session = ""
        self.requests: list[dict[str, object]] = []

    def close(self) -> None:
        self.client.close()

    def _post(self, body: dict[str, object], *, session: bool = True) -> httpx.Response:
        headers = {
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {self.token}",
            "content-type": "application/json",
        }
        if session and self.session:
            headers |= {"mcp-session-id": self.session, "mcp-protocol-version": PROTOCOL}
        response = self.client.post(self.endpoint, headers=headers, json=body)
        self.requests.append({"request": body, "http_status": response.status_code})
        return response

    def initialize(self) -> None:
        response = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "switchstand-semantic-probe", "version": "1"},
                "protocolVersion": PROTOCOL, "capabilities": {},
            },
        }, session=False)
        response.raise_for_status()
        self.session = response.headers["mcp-session-id"]
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}).raise_for_status()

    def call(self, method: str, params: dict[str, object], request_id: int) -> object:
        response = self._post({
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        })
        response.raise_for_status()
        payload = cast(dict[str, object], response.json())
        if "error" in payload:
            raise ProbeFailure(f"{method} returned JSON-RPC error")
        self.requests[-1]["response"] = payload
        result = cast(dict[str, object], payload["result"])
        return result.get("structuredContent", result)

    def unauthorized_initialize(self) -> int:
        response = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "wrong-token", "version": "1"},
                       "protocolVersion": PROTOCOL, "capabilities": {}},
        }, session=False)
        return response.status_code


def _status(result: object, expected: str, label: str) -> dict[str, object]:
    value = cast(dict[str, object], result) if isinstance(result, dict) else {}
    if value.get("status") != expected:
        raise ProbeFailure(f"{label} did not return {expected}")
    return value


def _runtime(client: httpx.Client, endpoint: str, sha: str) -> dict[str, object]:
    parsed = urlparse(endpoint)
    url = f"{parsed.scheme}://{parsed.netloc}/.well-known/switchstand-certification-runtime"
    response = client.get(url)
    response.raise_for_status()
    value = cast(dict[str, object], response.json())
    if value.get("runtime_sha") != sha or not value.get("run_id"):
        raise ProbeFailure("runtime identity does not match candidate")
    return value


def _publish(path: Path, value: object) -> None:
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--wrong-token-file", required=True, type=Path)
    parser.add_argument("--work-id", required=True, type=UUID)
    parser.add_argument("--foreign-work-id", required=True, type=UUID)
    parser.add_argument("--dependency-work-id", required=True, type=UUID)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-tools-sha256", required=True)
    parser.add_argument("--expected-principal", required=True, help="issuer|subject|client_id")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--allow-disposable-mutation", action="store_true")
    parser.add_argument("--operation-id", type=UUID)
    parser.add_argument("--expected-qualification")
    parser.add_argument("--restart-of", type=Path)
    args = parser.parse_args(argv)
    endpoint = str(args.endpoint)
    parsed = urlparse(endpoint)
    if (not parsed.hostname or parsed.path != "/mcp" or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise ProbeFailure("endpoint must be an exact credential-free /mcp URL")
    if parsed.scheme not in {"https", "http"} or (parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1", "localhost", "::1",
    }):
        raise ProbeFailure("endpoint must use HTTPS or loopback HTTP")
    actual_sha = subprocess.run(
        ["git", "-C", str(args.repo), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    if actual_sha != args.expected_sha:
        raise ProbeFailure("checkout does not match expected candidate")
    if args.restart_of is not None and not args.allow_disposable_mutation:
        raise ProbeFailure("--restart-of requires explicitly enabled disposable mutation")

    token, wrong = _private(args.token_file), _private(args.wrong_token_file)
    if not token or not wrong or token == wrong:
        raise ProbeFailure("distinct non-empty token files are required")
    client = MCP(endpoint, token)
    results: dict[str, object] = {}
    try:
        runtime = _runtime(client.client, endpoint, str(args.expected_sha))
        client.initialize()
        tools = cast(dict[str, object], client.call("tools/list", {}, 2))["tools"]
        if _digest(tools) != args.expected_tools_sha256:
            raise ProbeFailure("tools/list schema digest mismatch")
        got = _status(client.call("tools/call", {"name": "work_get", "arguments": {
            "api_version": "1", "work_id": str(args.work_id),
        }}, 3), "ok", "work_get")
        item = cast(dict[str, object], got["item"])
        revision = str(item["revision"])
        found = _status(client.call("tools/call", {"name": "work_search", "arguments": {
            "api_version": "1", "text": str(item["title"]),
        }}, 4), "ok", "work_search")
        rows = cast(list[dict[str, object]], found["items"])
        if str(args.work_id) not in {str(row["id"]) for row in rows}:
            raise ProbeFailure("work_search omitted target")
        dependency = _status(client.call("tools/call", {"name": "work_get", "arguments": {
            "api_version": "1", "work_id": str(args.dependency_work_id),
        }}, 5), "ok", "dependency work_get")
        stale = _status(client.call("tools/call", {"name": "work_structure", "arguments": {
            "api_version": "1", "work_id": str(args.work_id),
            "observed_revision": "semantic-probe-deliberately-stale",
        }}, 6), "stale", "stale currentness")
        foreign = _status(client.call("tools/call", {"name": "work_get", "arguments": {
            "api_version": "1", "work_id": str(args.foreign_work_id),
        }}, 7), "denied", "foreign WorkId")
        wrong_client = MCP(endpoint, wrong)
        try:
            wrong_token_status = wrong_client.unauthorized_initialize()
            if wrong_token_status not in {401, 403}:
                raise ProbeFailure("wrong token was not denied")
        finally:
            wrong_client.close()
        results = {"get": got, "search": found, "dependency": dependency,
                   "stale": stale, "foreign": foreign}

        mutation = None
        if args.allow_disposable_mutation:
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ProbeFailure("disposable mutation is hard-disabled outside loopback")
            if args.operation_id is None or not (args.expected_qualification or "").startswith(
                "test:disposable"
            ):
                raise ProbeFailure("mutation requires OperationId and disposable qualification")
            arguments = {"api_version": "1", "operation_id": str(args.operation_id),
                "work_id": str(args.work_id), "observed_revision": revision,
                "patch": {"kind": "dependency", "action": "add",
                          "target_work_id": str(args.dependency_work_id)}}
            first = _status(client.call("tools/call", {"name": "work_relate",
                "arguments": arguments}, 8), "ok", "dependency mutation")
            replay = _status(client.call("tools/call", {"name": "work_relate",
                "arguments": arguments}, 9), "ok", "OperationId replay")
            if first != replay:
                raise ProbeFailure("same OperationId replay changed receipt")
            receipt = cast(dict[str, object], first["receipt"])
            principal = receipt.get("principal", {})
            observed_principal = "|".join(str(cast(dict[str, object], principal).get(key, ""))
                for key in ("issuer", "subject", "client_id"))
            if (receipt.get("qualification") != args.expected_qualification
                    or observed_principal != args.expected_principal):
                raise ProbeFailure("mutation receipt principal or qualification mismatch")
            mutation = {"arguments": arguments, "first": first, "replay": replay}
            if args.restart_of:
                prior = json.loads(_private(args.restart_of))
                if prior.get("candidate_sha") != args.expected_sha or prior.get("mutation") != mutation:
                    raise ProbeFailure("cold-restart replay differs from prior receipt")

        _publish(args.receipt, {"schema": 1, "result": "PASS", "candidate_sha": args.expected_sha,
            "endpoint": endpoint, "principal": args.expected_principal,
            "runtime": runtime, "tools": tools, "tools_sha256": _digest(tools),
            "work_id": str(args.work_id), "dependency_work_id": str(args.dependency_work_id),
            "foreign_work_id": str(args.foreign_work_id), "revision": revision,
            "results": results, "results_sha256": _digest(results),
            "wrong_token_http_status": wrong_token_status,
            "mutation": mutation, "production_mutation": "NOT_RUN",
            "restart_of": None if args.restart_of is None else _digest(json.loads(_private(args.restart_of))),
            "transcript": client.requests})
        print("PASS authenticated semantic probe")
        return 0
    except (KeyError, OSError, TypeError, ValueError, httpx.HTTPError) as error:
        raise ProbeFailure(type(error).__name__) from error
    finally:
        client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except ProbeFailure as error:
        print(f"FAIL authenticated semantic probe: {error}", file=sys.stderr)
        raise SystemExit(1) from None
