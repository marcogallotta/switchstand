"""Qualify live Stateful currentness and emit one sealed immutable receipt."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
from uuid import UUID, uuid5

import httpx
from sqlalchemy.ext.asyncio import create_async_engine

from .grants import PrincipalContext
from .product_currentness import STATEFUL_PRODUCT_WORK_ID, evaluate_stateful_currentness
from .product_currentness_stateful import (
    LiveStatefulEvidenceReader,
    StatefulQualificationPayload,
    StatefulServerSnapshot,
    emit_stateful_qualification_receipt,
)
from .secure_file import create_new_private_bytes, read_private_bytes

PROTOCOL = "2025-03-26"
OPERATION_NAMESPACE = UUID("ec1a2a91-22de-5432-9791-da6acfed1346")


class QualificationFailure(RuntimeError):
    """A definite qualification failure that must never emit PASS."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _endpoint(value: str) -> str:
    parsed = urlparse(value)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        not parsed.hostname
        or parsed.path != "/mcp"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.scheme not in {"https", "http"}
        or (parsed.scheme == "http" and not loopback)
    ):
        raise QualificationFailure("endpoint must be credential-free HTTPS or loopback /mcp")
    return value


def _private_token(path: Path) -> str:
    token = read_private_bytes(path).decode().strip()
    if not token:
        raise QualificationFailure("token file must be nonempty")
    return token


def _attempt_directory(path: Path) -> None:
    if not path.is_absolute():
        raise QualificationFailure("attempt directory must be absolute")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise QualificationFailure("existing attempt directory must be mode-0700") from None


def _publish(path: Path, value: object) -> None:
    create_new_private_bytes(path, _canonical(value))


class MCPClient:
    """Small authenticated MCP client that retains no token or response transcript."""

    def __init__(self, endpoint: str, token: str):
        self.endpoint = endpoint
        self.token = token
        self.session = ""
        self._request_id = 0
        self.client = httpx.Client(timeout=15, trust_env=False, follow_redirects=False)

    def close(self) -> None:
        self.client.close()

    def _post(self, method: str, params: dict[str, object], *, session: bool = True) -> Any:
        self._request_id += 1
        headers = {
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {self.token}",
            "content-type": "application/json",
        }
        if session and self.session:
            headers |= {"mcp-session-id": self.session, "mcp-protocol-version": PROTOCOL}
        response = self.client.post(
            self.endpoint,
            headers=headers,
            json={"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params},
        )
        response.raise_for_status()
        if not session:
            self.session = response.headers["mcp-session-id"]
        payload = cast(dict[str, object], response.json())
        if "error" in payload:
            raise QualificationFailure(f"{method} returned a JSON-RPC error")
        return payload["result"]

    def initialize(self) -> None:
        self._post(
            "initialize",
            {
                "clientInfo": {"name": "switchstand-stateful-qualifier", "version": "1"},
                "protocolVersion": PROTOCOL,
                "capabilities": {},
            },
            session=False,
        )
        headers = {
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {self.token}",
            "content-type": "application/json",
            "mcp-session-id": self.session,
            "mcp-protocol-version": PROTOCOL,
        }
        response = self.client.post(
            self.endpoint,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        response.raise_for_status()

    def tools(self) -> list[dict[str, object]]:
        result = cast(dict[str, object], self._post("tools/list", {}))
        return cast(list[dict[str, object]], result["tools"])

    def call(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        result = cast(
            dict[str, object],
            self._post("tools/call", {"name": name, "arguments": arguments}),
        )
        value = result.get("structuredContent", result)
        if not isinstance(value, dict):
            raise QualificationFailure(f"{name} returned no structured result")
        return cast(dict[str, object], value)

    def runtime(self) -> dict[str, object]:
        parsed = urlparse(self.endpoint)
        url = f"{parsed.scheme}://{parsed.netloc}/.well-known/switchstand-certification-runtime"
        response = self.client.get(url)
        response.raise_for_status()
        return cast(dict[str, object], response.json())


def _status(value: dict[str, object], expected: str, label: str) -> dict[str, object]:
    if value.get("status") != expected:
        raise QualificationFailure(f"{label} did not return {expected}")
    return value


def _prepared(arguments: argparse.Namespace, item: dict[str, object]) -> dict[str, object]:
    attempt = cast(UUID, arguments.attempt_id)
    return {
        "schema": 1,
        "attempt_id": str(attempt),
        "endpoint": str(arguments.endpoint),
        "work_id": str(arguments.work_id),
        "denied_work_id": str(arguments.denied_work_id),
        "expected_runtime_sha": str(arguments.expected_runtime_sha),
        "expected_run_id": str(arguments.expected_run_id),
        "expected_tools_sha256": str(arguments.expected_tools_sha256),
        "observed_revision": item["revision"],
        "completed": item["completed"],
        "stale_operation_id": str(uuid5(OPERATION_NAMESPACE, f"{attempt}:stale")),
        "effect_operation_id": str(uuid5(OPERATION_NAMESPACE, f"{attempt}:effect")),
    }


def _load_or_prepare(path: Path, expected: dict[str, object]) -> dict[str, object]:
    journal = path / "prepared.json"
    try:
        _publish(journal, expected)
        return expected
    except FileExistsError:
        current = cast(dict[str, object], json.loads(read_private_bytes(journal)))
        replay_varying = {"observed_revision", "completed"}
        stable_expected = {
            key: value for key, value in expected.items() if key not in replay_varying
        }
        stable_current = {
            key: value for key, value in current.items() if key not in replay_varying
        }
        if stable_current != stable_expected or not {
            "observed_revision", "completed"
        } <= current.keys():
            raise QualificationFailure("existing attempt journal does not match exact arguments")
        return current


def _update_arguments(
    journal: dict[str, object], *, stale: bool,
) -> dict[str, object]:
    return {
        "api_version": "1",
        "work_id": journal["work_id"],
        "observed_revision": (
            "stateful-qualifier-deliberately-stale"
            if stale
            else journal["observed_revision"]
        ),
        "operation_id": (
            journal["stale_operation_id"] if stale else journal["effect_operation_id"]
        ),
        "patch": {"completed": journal["completed"]},
    }


async def _qualify(arguments: argparse.Namespace) -> dict[str, object]:
    endpoint = _endpoint(str(arguments.endpoint))
    token = _private_token(arguments.token_file)
    process = await asyncio.create_subprocess_exec(
        "git", "-C", str(arguments.repo), "rev-parse", "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    if process.returncode:
        raise QualificationFailure("cannot read checkout revision")
    actual_sha = stdout.decode().strip()
    if actual_sha != arguments.expected_runtime_sha:
        raise QualificationFailure("checkout does not match expected runtime SHA")
    if arguments.work_id == arguments.denied_work_id:
        raise QualificationFailure("denied WorkId must differ from the disposable WorkId")
    _attempt_directory(arguments.attempt_dir)
    client = MCPClient(endpoint, token)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        client.initialize()
        tools = client.tools()
        tools_digest = _digest(tools)
        if tools_digest != arguments.expected_tools_sha256:
            raise QualificationFailure("tools/list schema digest mismatch")
        runtime = client.runtime()
        if runtime != {
            "runtime_sha": arguments.expected_runtime_sha,
            "run_id": arguments.expected_run_id,
        }:
            raise QualificationFailure("certification runtime identity mismatch")
        principal = PrincipalContext(
            issuer=arguments.issuer,
            subject=arguments.subject,
            client_id=arguments.client_id,
            assurance="authenticated",
        )
        snapshot = StatefulServerSnapshot(
            runtime_sha=arguments.expected_runtime_sha,
            selected_runtime_sha=arguments.expected_runtime_sha,
            run_id=arguments.expected_run_id,
            principal_key=principal.key,
            outcome_actions_enabled="outcome_state_update" in {tool.get("name") for tool in tools},
            tool_names=tuple(str(tool["name"]) for tool in tools),
            tools_schema_sha256=tools_digest,
        )

        async def read_principal() -> PrincipalContext:
            return principal

        async def read_snapshot() -> StatefulServerSnapshot:
            return snapshot

        diagnostic = LiveStatefulEvidenceReader(
            engine,
            read_principal,
            read_snapshot,
            expected_migration_revision=arguments.expected_migration,
            expected_tools_schema_sha256=tools_digest,
        )
        before = await evaluate_stateful_currentness(STATEFUL_PRODUCT_WORK_ID, diagnostic)
        if before.current != "UNKNOWN" or before.blockers != ("functional_proof",):
            raise QualificationFailure("live prerequisites are not ready for functional proof")
        basis = await diagnostic.qualification_basis()
        got = _status(
            client.call("work_get", {"api_version": "1", "work_id": str(arguments.work_id)}),
            "ok",
            "authenticated work_get",
        )
        item = cast(dict[str, object], got.get("item"))
        if item.get("id") != str(arguments.work_id):
            raise QualificationFailure("disposable WorkId identity mismatch")
        expected_journal = _prepared(arguments, item)
        journal = _load_or_prepare(arguments.attempt_dir, expected_journal)
        denied = client.call(
            "work_get", {"api_version": "1", "work_id": str(arguments.denied_work_id)}
        )
        _status(denied, "denied", "foreign admission")
        stale = client.call("work_update", _update_arguments(journal, stale=True))
        _status(stale, "stale", "stale CAS")
        if stale.get("effect") != "not_sent":
            raise QualificationFailure("stale CAS did not prove no effect")
        update_arguments = _update_arguments(journal, stale=False)
        applied = client.call("work_update", update_arguments)
        _status(applied, "ok", "disposable update")
        if applied.get("effect") != "applied":
            raise QualificationFailure("disposable update was not applied")
        replay = client.call("work_update", update_arguments)
        if replay != applied:
            raise QualificationFailure("exact operation replay did not return the same receipt")
        receipt = cast(dict[str, object], applied.get("receipt"))
        resulting_revision = receipt.get("resulting_revision")
        rechecked = _status(
            client.call("work_get", {"api_version": "1", "work_id": str(arguments.work_id)}),
            "ok",
            "currentness recheck",
        )
        current_item = cast(dict[str, object], rechecked.get("item"))
        if not resulting_revision or current_item.get("revision") != resulting_revision:
            raise QualificationFailure("currentness recheck did not match the effect receipt")
        payload = StatefulQualificationPayload(
            schema=2,
            issuer="switchstand-stateful-qualifier",
            qualification="real:authenticated-stateful-currentness-v1",
            result="PASS",
            runtime_sha=snapshot.runtime_sha,
            selected_runtime_sha=snapshot.selected_runtime_sha,
            run_id=snapshot.run_id,
            principal_key=snapshot.principal_key,
            tools_schema_sha256=snapshot.tools_schema_sha256,
            persistence_token=basis.persistence_token,
            basis_token=basis.basis_token,
            contract_token=basis.contract_token,
            authenticated_mcp="PASS",
            admission="DENIED",
            stale_cas="STALE",
            replay="REPLAYED",
            currentness="RECHECKED",
            observed_at=datetime.now(UTC),
        )
        try:
            emission = emit_stateful_qualification_receipt(
                payload,
                key_path=arguments.key,
                receipt_path=arguments.receipt,
            )
            receipt_digest = emission.digest
        except FileExistsError:
            receipt_digest = hashlib.sha256(read_private_bytes(arguments.receipt)).hexdigest()
        qualified = LiveStatefulEvidenceReader(
            engine,
            read_principal,
            read_snapshot,
            expected_migration_revision=arguments.expected_migration,
            expected_tools_schema_sha256=tools_digest,
            qualification_receipt=arguments.receipt,
            qualification_key=arguments.key,
        )
        after = await evaluate_stateful_currentness(STATEFUL_PRODUCT_WORK_ID, qualified)
        if (after.status, after.current, after.blockers) != ("ok", "TRUE", ()):
            raise QualificationFailure("sealed receipt did not re-evaluate to TRUE")
        result: dict[str, object] = {
            "schema": 1,
            "result": "PASS",
            "attempt_id": str(arguments.attempt_id),
            "receipt_digest": receipt_digest,
            "runtime_sha": snapshot.runtime_sha,
            "run_id": snapshot.run_id,
            "tools_schema_sha256": tools_digest,
            "work_id": str(arguments.work_id),
            "denied_work_id": str(arguments.denied_work_id),
            "authenticated_mcp": "PASS",
            "admission": "DENIED",
            "stale_cas": "STALE",
            "replay": "REPLAYED",
            "currentness": "RECHECKED",
        }
        try:
            _publish(arguments.attempt_dir / "result.json", result)
        except FileExistsError:
            existing = json.loads(read_private_bytes(arguments.attempt_dir / "result.json"))
            if existing != result:
                raise QualificationFailure("existing terminal result does not match exact replay")
        return result
    finally:
        client.close()
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    digest = commands.add_parser("schema-digest")
    digest.add_argument("--endpoint", required=True)
    digest.add_argument("--token-file", required=True, type=Path)
    qualify = commands.add_parser("qualify")
    qualify.add_argument("--endpoint", required=True)
    qualify.add_argument("--token-file", required=True, type=Path)
    qualify.add_argument("--issuer", required=True)
    qualify.add_argument("--subject", required=True)
    qualify.add_argument("--client-id", required=True)
    qualify.add_argument("--work-id", required=True, type=UUID)
    qualify.add_argument("--denied-work-id", required=True, type=UUID)
    qualify.add_argument("--attempt-id", required=True, type=UUID)
    qualify.add_argument("--attempt-dir", required=True, type=Path)
    qualify.add_argument("--expected-runtime-sha", required=True)
    qualify.add_argument("--expected-run-id", required=True)
    qualify.add_argument("--expected-tools-sha256", required=True)
    qualify.add_argument("--expected-migration", default="0023_activation_continuity")
    qualify.add_argument("--key", required=True, type=Path)
    qualify.add_argument("--receipt", required=True, type=Path)
    qualify.add_argument("--repo", type=Path, default=Path.cwd())
    return parser


def main() -> None:
    parser = _parser()
    arguments = parser.parse_args()
    try:
        if arguments.command == "schema-digest":
            client = MCPClient(_endpoint(arguments.endpoint), _private_token(arguments.token_file))
            try:
                client.initialize()
                print(_digest(client.tools()))
            finally:
                client.close()
            return
        print(json.dumps(asyncio.run(_qualify(arguments)), sort_keys=True))
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        httpx.HTTPError,
        subprocess.CalledProcessError,
        QualificationFailure,
    ) as error:
        parser.exit(1, f"Stateful qualification failed: {type(error).__name__}\n")


if __name__ == "__main__":
    main()
