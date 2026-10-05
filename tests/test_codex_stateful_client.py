import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn
from chatgpt_fixture import ACTIVE, REFERENCE, grant, service
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AccessToken
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.chatgpt_edge import (
    REQUIRED_SCOPE,
    MCPAuthConfig,
    SwitchstandGitHubProvider,
    create_app,
    runtime_identity_from_meta,
)
from switchstand.core import UnknownEffect
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext
from switchstand.messages import MessageState
from switchstand.state import PostgresState, metadata

RESOURCE = "https://switchstand.example.com/mcp"
ISSUER = "https://switchstand.example.com/"
GITHUB_ID = "192548"
CONFIG = MCPAuthConfig("client", "secret", GITHUB_ID, RESOURCE)
SERVER_NAME = "switchstand_proof"


@pytest.mark.parametrize(("meta", "expected"), [
    ({"openai/session": "chat"}, "chat"),
    ({"threadId": "thread"}, "codex:thread"),
    ({}, ""),
    ({"threadId": ""}, ""),
    ({"threadId": 1}, ""),
    ({"openai/session": "chat", "threadId": "thread"}, ""),
])
def test_runtime_identity_accepts_exactly_one_supported_host_identity(
    meta: dict[str, object], expected: str,
) -> None:
    assert runtime_identity_from_meta(meta) == expected


class AppServerClient:
    def __init__(self, binary: Path, workspace: Path, env: dict[str, str]) -> None:
        self.next_id = 1
        self.process = subprocess.Popen(
            [str(binary), "app-server"],
            cwd=workspace,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

    def send(self, message: dict[str, object]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str) -> None:
        self.send({"method": method})

    def request(self, method: str, params: dict[str, object] | None) -> dict[str, object]:
        request_id = self.next_id
        self.next_id += 1
        request: dict[str, object] = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        self.send(request)
        assert self.process.stdout is not None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if not line:
                break
            message = json.loads(line)
            if message.get("id") == request_id and "method" not in message:
                return message
            if "id" in message and isinstance(message.get("method"), str):
                self.send({
                    "id": message["id"],
                    "error": {"code": -32601, "message": "proof client does not handle server requests"},
                })
        stderr = ""
        if self.process.stderr is not None and self.process.poll() is not None:
            stderr = self.process.stderr.read()
        raise AssertionError(f"Codex app-server did not answer {method}: {stderr[-4000:]}")


def response_result(response: dict[str, object]) -> dict[str, object]:
    error = response.get("error")
    assert error is None, error
    result = response.get("result")
    assert isinstance(result, dict), response
    return result


def codex_smoke(binary: Path, workspace: Path, env: dict[str, str]) -> None:
    version = subprocess.run(
        [str(binary), "--version"], text=True, capture_output=True, check=True
    ).stdout.strip()
    print(f"CODEX_PROOF_VERSION={version}", flush=True)

    client = AppServerClient(binary, workspace, env)
    try:
        response_result(client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "switchstand-stateful-proof",
                    "title": "Switchstand stateful MCP proof",
                    "version": "1",
                },
                "capabilities": {"experimentalApi": True},
            },
        ))
        client.notify("initialized")

        inventory = response_result(client.request("mcpServerStatus/list", {"detail": "full"}))
        entries = inventory.get("data")
        assert isinstance(entries, list)
        entry = next(
            item for item in entries
            if isinstance(item, dict) and item.get("name") == SERVER_NAME
        )
        tools = entry.get("tools")
        assert isinstance(tools, dict), entry
        assert {"work_get", "work_update", "outcome_state_update"} <= tools.keys()
        assert "grant_get" not in tools

        thread = response_result(client.request(
            "thread/start", {"cwd": str(workspace), "ephemeral": True}
        ))
        thread_data = thread.get("thread")
        assert isinstance(thread_data, dict)
        thread_id = thread_data.get("id")
        assert isinstance(thread_id, str)

        def call(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            called = response_result(client.request(
                "mcpServer/tool/call",
                {
                    "threadId": thread_id,
                    "server": SERVER_NAME,
                    "tool": tool,
                    "arguments": arguments,
                },
            ))
            structured = called.get("structuredContent")
            assert isinstance(structured, dict), called
            return structured

        def get(work_id=ACTIVE) -> dict[str, object]:
            result = call("work_get", {"api_version": "1", "work_id": str(work_id)})
            assert result["status"] == "ok", result
            item = result["item"]
            assert isinstance(item, dict) and item["id"] == str(work_id)
            return result

        def write(
            observed_revision: str, items: list[dict[str, object]],
            expected_state_id: str | None,
        ) -> str:
            result = call("outcome_state_update", {
                "api_version": "1", "operation_id": str(uuid4()),
                "owner_work_id": str(ACTIVE), "expected_state_id": expected_state_id,
                "owner_observed_revision": observed_revision, "items": items,
            })
            assert result["status"] == "APPLIED", result
            state_id = result["state_id"]
            assert isinstance(state_id, str)
            return state_id

        def update(observed_revision: str, patch: dict[str, object]) -> dict[str, object]:
            result = call("work_update", {
                "api_version": "1", "operation_id": str(uuid4()),
                "work_id": str(ACTIVE), "observed_revision": observed_revision,
                "patch": patch,
            })
            return result

        def summary(
            result: dict[str, object], currentness: str,
            action_class: str | None,
        ) -> None:
            value = result["action_summary"]
            assert isinstance(value, dict) and value["currentness"] == currentness
            actions = value["actions"]
            assert isinstance(actions, list)
            assert value["open_action_count"] == len(actions)
            assert [action["action_class"] for action in actions] == (
                [] if action_class is None else [action_class]
            )

        # The real client sees every governing CURRENT/STALE action shape. Model-response
        # frequency targets remain activation evidence, not an inert CI claim.
        initial = get()
        assert "action_summary" not in initial
        needs_marco = {
            "item_key": "decision", "description": "Choose rollout", "kind": "DECISION",
            "status": "READY", "who_acts": "MARCO",
            "what_yes_causes": "Dispatch implementation", "source_label": "AGENT",
        }
        state_id = write("r1", [needs_marco], None)
        summary(get(), "CURRENT", "NEEDS_MARCO")
        assert "action_summary" not in get(REFERENCE)
        changed = update("r1", {"completed": True})
        assert changed["status"] == "ok", changed
        summary(changed, "STALE", "NEEDS_MARCO")

        owner_can_do = {
            "item_key": "owner", "description": "Continue implementation",
            "kind": "DELIVERABLE", "status": "READY", "who_acts": "OWNER",
            "source_label": "AGENT",
        }
        state_id = write("r2", [owner_can_do], state_id)
        summary(get(), "CURRENT", "OWNER_CAN_DO")
        changed = update("r2", {"completed": False})
        summary(changed, "STALE", "OWNER_CAN_DO")

        done = owner_can_do | {"status": "DONE"}
        state_id = write("r3", [done], state_id)
        summary(get(), "CURRENT", None)
        changed = update("r3", {"completed": True})
        summary(changed, "STALE", None)

        ready_to_dispatch = {
            "item_key": "dispatch", "description": "Dispatch reviewed package",
            "kind": "DELIVERABLE", "status": "READY", "who_acts": "MARCO",
            "dispatch_work_id": str(REFERENCE), "source_label": "AGENT",
        }
        write("r4", [ready_to_dispatch], state_id)
        summary(get(), "CURRENT", "READY_TO_DISPATCH")
        changed = update("r4", {"completed": False})
        summary(changed, "STALE", "READY_TO_DISPATCH")

        unknown = update("r5", {"notes": "inject unknown"})
        assert unknown["status"] == "unknown" and unknown["effect"] == "unknown"
        assert "action_summary" not in unknown

        registered = response_result(client.request(
            "mcpServer/tool/call",
            {
                "threadId": thread_id,
                "server": SERVER_NAME,
                "tool": "agent_register",
                "arguments": {"api_version": "1", "name": "codex-proof"},
            },
        ))["structuredContent"]
        assert isinstance(registered, dict) and registered["status"] == "ok", registered
        pending = response_result(client.request(
            "mcpServer/tool/call",
            {
                "threadId": thread_id,
                "server": SERVER_NAME,
                "tool": "agent_message_pending",
                "arguments": {"api_version": "1"},
            },
        ))["structuredContent"]
        assert isinstance(pending, dict) and pending["status"] == "ok", pending
    finally:
        client.close()


@pytest.mark.asyncio
async def test_current_codex_app_server_calls_work_get_over_stateful_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_prerequisite,
) -> None:
    raw_binary = os.getenv("CODEX_EXEC_PATH")
    if not raw_binary:
        pytest.skip(
            "NOT_RUN: real Codex binary is supplied only by the activation proof workflow"
        )
    binary = Path(raw_binary)
    assert binary.is_file() and os.access(binary, os.X_OK)

    async def verified(_self, token: str) -> AccessToken | None:
        if token != "proof-token":
            return None
        return AccessToken(
            token=token,
            client_id="codex-proof",
            scopes=[REQUIRED_SCOPE],
            subject=GITHUB_ID,
            claims={"iss": ISSUER},
            resource=RESOURCE,
            expires_at=int(time.time()) + 300,
        )

    monkeypatch.setattr(SwitchstandGitHubProvider, "verify_token", verified)
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("NOT_RUN: real Codex proof also requires disposable PostgreSQL")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, activation_obligation_revisions"
        ))
        await connection.run_sync(metadata.create_all)
        await connection.execute(text(
            "INSERT INTO work_handles (id, provider, provider_work_id) "
            "VALUES (:active, 'asana', '123'), (:reference, 'asana', '456')"
        ), {"active": ACTIVE, "reference": REFERENCE})
    subject = service()
    subject.state = PostgresState(engine)
    subject.outcome_state_enabled = True
    subject.messages = MessageState(engine, GrantState(engine))
    subject.grants.grant = grant(
        principal=PrincipalContext(
            issuer=ISSUER,
            subject=GITHUB_ID,
            client_id="codex-proof",
            assurance="authenticated",
        ),
        scope="workspace",
        operations=frozenset({"work_get", "work_update"}),
        update_qualification="real:ordinary-workspace",
    )
    provider = subject.providers["asana"]
    original_update = provider.update

    async def update_with_unknown(task_gid, patch):
        if patch.notes == "inject unknown":
            raise UnknownEffect("injected lost response")
        await original_update(task_gid, patch)

    monkeypatch.setattr(provider, "update", update_with_unknown)
    app = create_app(subject, CONFIG, client_storage=MemoryStore())

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started

        codex_home = tmp_path / "codex-home"
        workspace = tmp_path / "workspace"
        codex_home.mkdir()
        workspace.mkdir()
        (codex_home / "config.toml").write_text(
            'approval_policy = "never"\n'
            f'[mcp_servers.{SERVER_NAME}]\n'
            f'url = "http://127.0.0.1:{port}/mcp"\n'
            'bearer_token_env_var = "SWITCHSTAND_PROOF_TOKEN"\n'
            'required = true\n'
            'default_tools_approval_mode = "approve"\n'
            'enabled_tools = ["work_get", "work_update", "outcome_state_update", '
            '"agent_register", "agent_message_pending"]\n'
        )
        env = dict(os.environ)
        env["CODEX_HOME"] = str(codex_home)
        env["SWITCHSTAND_PROOF_TOKEN"] = "proof-token"
        await asyncio.to_thread(codex_smoke, binary, workspace, env)
    finally:
        server.should_exit = True
        await server_task
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
            await connection.execute(text(
                "DROP TABLE IF EXISTS alembic_version, activation_obligation_revisions"
            ))
        await engine.dispose()
