"""Deterministic isolated CREATE/restart qualification; never a production activator."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from alembic import command
from alembic.config import Config as AlembicConfig
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import create_async_engine

from .core import provision_launch
from .grant_state import GrantState
from .grants import PrincipalContext, WorkGrant
from .provider import WORKSPACE, AsanaProvider
from .state import PostgresState

TOOLS = {"grant_get", "work_get", "source_task", "source_stories", "source_story",
         "work_append", "work_create"}


class Blocked(RuntimeError):
    """A missing prerequisite, distinct from candidate failure."""


@dataclass(frozen=True, slots=True)
class Inputs:
    parent_task: str
    test_project: str
    correlation_field: str
    negative_task: str
    database_url: str
    auth_endpoint: str
    auth_token: str
    timeout: int
    keep_task: bool


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-task", required=True)
    parser.add_argument("--test-project", required=True)
    parser.add_argument("--correlation-field", required=True)
    parser.add_argument("--negative-task", required=True)
    parser.add_argument("--database-url", default=os.getenv("TEST_DATABASE_URL", ""))
    parser.add_argument("--auth-endpoint", default=os.getenv("SWITCHSTAND_TEST_AUTH_ENDPOINT", ""))
    parser.add_argument("--auth-token", default=os.getenv("SWITCHSTAND_TEST_AUTH_TOKEN", ""))
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--keep-task", action="store_true")
    return parser


def _inputs(args: argparse.Namespace) -> Inputs:
    values = (args.parent_task, args.test_project, args.correlation_field, args.negative_task)
    if any(not value.isdigit() for value in values):
        raise Blocked("all Asana identifiers must be numeric")
    if not args.database_url:
        raise Blocked("TEST_DATABASE_URL must name disposable switchstand_test")
    try:
        url = make_url(args.database_url)
    except ArgumentError as error:
        raise Blocked("TEST_DATABASE_URL is invalid") from error
    if url.database != "switchstand_test":
        raise Blocked("database must be disposable switchstand_test")
    if not args.auth_endpoint or not args.auth_token:
        raise Blocked("real authenticated edge endpoint/token is required")
    return Inputs(*values, args.database_url, args.auth_endpoint, args.auth_token,
                  args.timeout, args.keep_task)


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _auth_smoke(inputs: Inputs) -> PrincipalContext:
    transport = StreamableHttpTransport(inputs.auth_endpoint, auth=inputs.auth_token)
    async with Client(transport) as client:
        tools = {tool.name for tool in await client.list_tools()}
        if not TOOLS.issubset(tools):
            raise Blocked(f"authenticated edge missing tools: {sorted(TOOLS - tools)}")
        result = await client.call_tool("grant_get", {"api_version": "1"})
        content = result.structured_content
        if not isinstance(content, dict) or not isinstance(content.get("principal"), dict):
            raise Blocked("authenticated edge did not return its verified principal")
        return PrincipalContext.model_validate(content["principal"])


async def _asana_get(client: httpx.AsyncClient, path: str, **params: str) -> dict[str, object]:
    response = await client.get(path, params=params or None)
    response.raise_for_status()
    body = cast(dict[str, object], response.json())
    payload = body.get("data")
    if not isinstance(payload, dict):
        raise Blocked(f"invalid Asana response for {path}")
    return cast(dict[str, object], payload)


def _memberships(task: dict[str, object]) -> set[str]:
    result: set[str] = set()
    rows = task.get("memberships")
    if not isinstance(rows, list):
        return result
    for raw_row in cast(list[object], rows):
        if not isinstance(raw_row, dict):
            continue
        row = cast(dict[str, object], raw_row)
        raw_project = row.get("project")
        if isinstance(raw_project, dict):
            project = cast(dict[str, object], raw_project)
            if "gid" in project:
                result.add(str(project["gid"]))
    return result


async def _preflight(inputs: Inputs, client: httpx.AsyncClient) -> None:
    parent = await _asana_get(client, f"/tasks/{inputs.parent_task}",
                              opt_fields="gid,parent,memberships.project.gid")
    negative = await _asana_get(client, f"/tasks/{inputs.negative_task}",
                                opt_fields="gid,memberships.project.gid")
    if inputs.test_project not in _memberships(parent):
        raise Blocked("parent is not in the admitted test project")
    if inputs.test_project in _memberships(negative):
        raise Blocked("negative control is inside the admitted test project")
    settings = await client.get(
        f"/projects/{inputs.test_project}/custom_field_settings",
        params={"opt_fields": "custom_field.gid,custom_field.resource_subtype"},
    )
    settings.raise_for_status()
    body = cast(dict[str, object], settings.json())
    rows = cast(list[dict[str, dict[str, object]]], body.get("data", []))
    matching = [row for row in rows if str(row.get("custom_field", {}).get("gid"))
                == inputs.correlation_field]
    if (len(matching) != 1
            or matching[0]["custom_field"].get("resource_subtype") != "text"):
        raise Blocked("correlation field is not one enabled text field on the test project")


async def _search(client: httpx.AsyncClient, inputs: Inputs, operation_id: UUID) -> list[str]:
    response = await client.get(f"/workspaces/{WORKSPACE}/tasks/search", params={
        f"custom_fields.{inputs.correlation_field}.value": str(operation_id),
        "projects.any": inputs.test_project, "limit": "2", "opt_fields": "gid",
    })
    response.raise_for_status()
    payload = cast(dict[str, object], response.json())
    if payload.get("next_page") is not None:
        raise Blocked("correlation search is not bounded to one page")
    rows = cast(list[dict[str, object]], payload.get("data", []))
    return [str(row["gid"]) for row in rows]


async def _provision(inputs: Inputs, principal: PrincipalContext) -> tuple[WorkGrant, UUID]:
    config = AlembicConfig("alembic.ini")
    config.set_main_option("sqlalchemy.url", inputs.database_url)
    command.upgrade(config, "head")
    engine = create_async_engine(inputs.database_url)
    try:
        state, grants = PostgresState(engine), GrantState(engine)
        async with httpx.AsyncClient(
            base_url="https://app.asana.com/api/1.0", trust_env=False,
            headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"},
        ) as client:
            authority = await provision_launch(
                state, "asana", AsanaProvider(client, inputs.test_project, test_only=True),
                inputs.parent_task, (),
            )
        negative = await state.bind("asana", inputs.negative_task)
        current = await grants.current(principal.key)
        version = 1 if current is None else current.version + 1
        grant = WorkGrant(
            id=uuid4(), version=version, principal=principal, authority=authority,
            operations=frozenset({"work_get", "work_create"}), issuer="create-canary",
            provenance=f"isolated project {inputs.test_project}",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            create_qualification=f"test:project:{inputs.test_project}",
        )
        await grants.issue(grant, None if current is None else current.version)
        return grant, negative.id
    finally:
        await engine.dispose()


@asynccontextmanager
async def _edge(inputs: Inputs, *, lose: bool, field: str | None = None
                ) -> AsyncGenerator[str]:
    port = _port()
    env = os.environ.copy() | {
        "DATABASE_URL": inputs.database_url,
        "SWITCHSTAND_TEST_PROJECT_GID": inputs.test_project,
        "SWITCHSTAND_CREATE_CORRELATION_FIELD_GID": field or inputs.correlation_field,
        "SWITCHSTAND_TEST_LOSE_CREATE_CONFIRMATION": "1" if lose else "0",
        "SWITCHSTAND_MCP_BIND_HOST": "127.0.0.1",
        "SWITCHSTAND_MCP_BIND_PORT": str(port),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "switchstand.chatgpt_edge",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env,
    )
    endpoint = f"http://127.0.0.1:{port}/mcp"
    try:
        async with httpx.AsyncClient(trust_env=False) as probe:
            for _ in range(400):
                if process.returncode is not None:
                    assert process.stderr is not None
                    error = (await process.stderr.read()).decode()
                    raise Blocked(f"edge exited during startup: {error[-1000:]}")
                try:
                    if (await probe.get(
                        f"http://127.0.0.1:{port}/.well-known/oauth-authorization-server"
                    )).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise Blocked("edge did not become ready")
        yield endpoint
    finally:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.kill()
            await process.wait()


async def _call(inputs: Inputs, endpoint: str, tool: str,
                arguments: dict[str, Any]) -> dict[str, Any]:
    async with Client(StreamableHttpTransport(endpoint, auth=inputs.auth_token)) as client:
        result = await client.call_tool(tool, {"api_version": "1"} | arguments)
        if not isinstance(result.structured_content, dict):
            raise Blocked(f"{tool} returned no structured result")
        return result.structured_content


async def _run(inputs: Inputs, report: dict[str, Any]) -> dict[str, Any]:
    principal = await _auth_smoke(inputs)
    token = os.environ.get("ASANA_TOKEN", "")
    if not token:
        raise Blocked("ASANA_TOKEN is required")
    async with httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", trust_env=False,
        headers={"Authorization": f"Bearer {token}"},
    ) as asana:
        await _preflight(inputs, asana)
        operation_id = uuid4()
        report["operation_id"] = str(operation_id)
        if await _search(asana, inputs, operation_id):
            raise Blocked("operation correlation exists before CREATE")
        grant, negative_work_id = await _provision(inputs, principal)
        request = {
            "operation_id": str(operation_id),
            "parent_work_id": str(grant.authority.active_work_id), "grant_version": grant.version,
            "title": "SWITCHSTAND CREATE RECOVERY CANARY",
            "notes": f"isolated deterministic canary operation {operation_id}",
        }
        async with _edge(inputs, lose=True) as edge:
            active = await _call(inputs, edge, "work_get", {})
            denied = await _call(inputs, edge, "work_get", {"work_id": str(negative_work_id)})
            report["possible_provider_effect"] = True
            first = await _call(inputs, edge, "work_create", request)
        await asyncio.sleep(30)
        matches = await _search(asana, inputs, operation_id)
        report["correlated_task_gids"] = matches
        if (active.get("status") != "ok" or denied.get("status") != "denied"
                or first.get("effect") != "unknown" or len(matches) != 1):
            raise AssertionError("read/isolation/ambiguous CREATE invariant failed")
        wrong_field = inputs.correlation_field + "0"
        async with _edge(inputs, lose=False, field=wrong_field) as edge:
            fenced = await _call(inputs, edge, "work_create", request)
        if fenced.get("effect") != "unknown" or await _search(asana, inputs, operation_id) != matches:
            raise AssertionError("incompatible recovery configuration was not fenced")
        async with _edge(inputs, lose=False) as edge:
            recovered = await _call(inputs, edge, "work_create", request)
            readback = await _call(
                inputs, edge, "work_get", {"work_id": recovered.get("work_id")},
            )
        final_matches = await _search(asana, inputs, operation_id)
        if (recovered.get("status") != "ok" or readback.get("status") != "ok"
                or final_matches != matches):
            raise AssertionError("restart recovery did not converge on one exact task")
        return {
            "verdict": "PASS", "operation_id": str(operation_id),
            "work_id": recovered.get("work_id"), "task_gid": matches[0],
            "isolation": "PASS", "restart_recovery": "PASS", "auth_smoke": "PASS",
        }


async def _evidence_cleanup(inputs: Inputs, report: dict[str, Any]) -> None:
    raw_operation = report.get("operation_id")
    if not isinstance(raw_operation, str):
        return
    operation_id = UUID(raw_operation)
    async with httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", trust_env=False,
        headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"},
    ) as asana:
        matches = await _search(asana, inputs, operation_id)
        report["correlated_task_gids"] = matches
        report["correlated_task_count"] = len(matches)
        report["observed_post_count"] = None
        owned: list[str] = []
        for gid in matches:
            task = await _asana_get(
                asana, f"/tasks/{gid}",
                opt_fields="gid,parent.gid,memberships.project.gid,custom_fields.gid,custom_fields.text_value",
            )
            parent = task.get("parent")
            parent_gid = cast(dict[str, object], parent).get("gid") if isinstance(parent, dict) else None
            fields = cast(list[dict[str, object]], task.get("custom_fields", []))
            correlation = next((field.get("text_value") for field in fields
                                if str(field.get("gid")) == inputs.correlation_field), None)
            if (parent_gid == inputs.parent_task
                    and inputs.test_project in _memberships(task)
                    and correlation == raw_operation):
                owned.append(gid)
        report["owned_correlated_task_gids"] = owned
        if inputs.keep_task:
            report["cleanup"] = "kept"
        elif owned != matches:
            report["cleanup"] = "withheld_unverified_ownership"
        else:
            for gid in owned:
                response = await asana.delete(f"/tasks/{gid}")
                response.raise_for_status()
            report["cleanup"] = "deleted"


async def execute(inputs: Inputs) -> dict[str, Any]:
    report: dict[str, Any] = {}
    try:
        async with asyncio.timeout(inputs.timeout):
            outcome = await _run(inputs, report)
    except Blocked as error:
        outcome = {"verdict": "BLOCKED", "reason": str(error)}
    except httpx.HTTPError as error:
        outcome = {"verdict": "BLOCKED", "reason": f"external test dependency unavailable: {error}"}
    except TimeoutError:
        outcome = {"verdict": "BLOCKED", "reason": "overall timeout exceeded"}
    except Exception as error:  # noqa: BLE001 - the CLI must always return structured truth.
        outcome = {"verdict": "FAIL", "reason": f"{type(error).__name__}: {error}"}
    try:
        await asyncio.wait_for(_evidence_cleanup(inputs, report), 10)
    except Exception as error:  # noqa: BLE001 - preserve the primary result and cleanup truth.
        report["cleanup"] = f"failed:{type(error).__name__}"
        if outcome["verdict"] == "PASS":
            outcome = {"verdict": "FAIL", "reason": "evidence preservation or cleanup failed"}
    if outcome["verdict"] == "PASS" and report.get("correlated_task_count") != 1:
        outcome = {"verdict": "FAIL", "reason": "correlated provider effect count was not one"}
    return outcome | report


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        inputs = _inputs(args)
    except (Blocked, ValueError) as error:
        print(json.dumps({"verdict": "BLOCKED", "reason": str(error)}, sort_keys=True))
        raise SystemExit(2) from None
    result = asyncio.run(execute(inputs))
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["verdict"] == "PASS" else 2)
