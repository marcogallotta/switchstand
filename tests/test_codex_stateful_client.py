import asyncio
import json
import os
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import uvicorn
from chatgpt_fixture import Provider, grant, service
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AccessToken

from switchstand.chatgpt_edge import (
    REQUIRED_SCOPE,
    MCPAuthConfig,
    SwitchstandGitHubProvider,
    create_app,
)
from switchstand.chatgpt import ChatGPTService
from switchstand.contracts import LaunchAuthority, Routing, WorkContext
from switchstand.core import (
    ProviderError,
    ProviderRelation,
    ProviderSourceStory,
    ProviderSourceTask,
    ProviderStoriesPage,
    ProviderWork,
)
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext, WorkGrant
from switchstand.lifecycle import LifecycleRepository, RequiredResultPersistence
from switchstand.messages import MessageState
from switchstand.resolver import REGISTRY_TASK_GID
from switchstand.state import PostgresState, metadata
from sqlalchemy.ext.asyncio import create_async_engine

RESOURCE = "https://switchstand.example.com/mcp"
ISSUER = "https://switchstand.example.com/"
GITHUB_ID = "192548"
CONFIG = MCPAuthConfig("client", "secret", GITHUB_ID, RESOURCE)
SERVER_NAME = "switchstand_proof"


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


def codex_tool(
    client: AppServerClient, thread_id: str, server: str, tool: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    called = response_result(client.request(
        "mcpServer/tool/call",
        {
            "threadId": thread_id,
            "server": server,
            "tool": tool,
            "arguments": arguments,
        },
    ))
    structured = called.get("structuredContent")
    assert isinstance(structured, dict), called
    return structured


def codex_full_journey(
    binary: Path, workspace: Path, env: dict[str, str],
    alpha_work_id: UUID, beta_work_id: UUID,
) -> None:
    client = AppServerClient(binary, workspace, env)
    try:
        response_result(client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "switchstand-phase1-proof",
                    "title": "Switchstand Phase 1 proof",
                    "version": "1",
                },
                "capabilities": {"experimentalApi": True},
            },
        ))
        client.notify("initialized")

        inventory = response_result(client.request("mcpServerStatus/list", {"detail": "full"}))
        entries = inventory.get("data")
        assert isinstance(entries, list)
        names = {
            item.get("name") for item in entries
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        assert {"switchstand_alpha", "switchstand_beta", "switchstand_beta_replacement"} <= names
        assert not any("asana" in name.casefold() for name in names)

        thread = response_result(client.request(
            "thread/start", {"cwd": str(workspace), "ephemeral": True}
        ))
        thread_data = thread.get("thread")
        assert isinstance(thread_data, dict)
        thread_id = thread_data.get("id")
        assert isinstance(thread_id, str)

        alpha, beta, replacement = (
            "switchstand_alpha", "switchstand_beta", "switchstand_beta_replacement"
        )
        assert codex_tool(client, thread_id, alpha, "agent_register", {
            "api_version": "1", "name": "Agent Alpha",
        })["status"] == "ok"
        assert codex_tool(client, thread_id, beta, "agent_register", {
            "api_version": "1", "name": "Agent Beta",
        })["status"] == "ok"

        resolved = codex_tool(client, thread_id, alpha, "work_resolve_alias", {
            "api_version": "1", "alias": "ChatGPT MCP",
        })
        assert resolved["status"] == "ok"
        hit = resolved.get("hit")
        assert isinstance(hit, dict)
        owner = hit.get("owner")
        assert isinstance(owner, dict) and owner["task_gid"] == "123"

        current = codex_tool(client, thread_id, alpha, "work_get", {
            "api_version": "1", "work_id": str(alpha_work_id),
        })
        assert current["status"] == "ok"
        item = current.get("item")
        assert isinstance(item, dict)
        revision = item["revision"]
        assert isinstance(revision, str)

        history = codex_tool(client, thread_id, alpha, "work_history", {
            "api_version": "1", "work_id": str(alpha_work_id),
            "observed_revision": revision, "limit": 10,
        })
        assert history["status"] == "ok"
        events = history.get("events")
        assert isinstance(events, list) and events

        created = codex_tool(client, thread_id, alpha, "work_create", {
            "api_version": "1", "operation_id": str(uuid4()),
            "title": "Phase 1 proof task", "notes": "created through MCP",
            "project_gid": "999",
        })
        assert created["status"] == "ok" and created["effect"] == "applied"
        created_work_id = created["work_id"]
        assert isinstance(created_work_id, str)

        created_read = codex_tool(client, thread_id, alpha, "work_get", {
            "api_version": "1", "work_id": created_work_id,
        })
        created_item = created_read.get("item")
        assert created_read["status"] == "ok" and isinstance(created_item, dict)
        created_revision = created_item["revision"]
        assert isinstance(created_revision, str)

        mutated = codex_tool(client, thread_id, alpha, "work_update", {
            "api_version": "1", "operation_id": str(uuid4()),
            "work_id": created_work_id, "observed_revision": created_revision,
            "patch": {"title": "Phase 1 mutated", "notes": "mutated through MCP"},
        })
        assert mutated["status"] == "ok" and mutated["effect"] == "applied"
        mutated_receipt = mutated.get("receipt")
        assert isinstance(mutated_receipt, dict)
        created_revision = mutated_receipt["resulting_revision"]

        moved = codex_tool(client, thread_id, alpha, "work_relate", {
            "api_version": "1", "operation_id": str(uuid4()),
            "work_id": created_work_id, "observed_revision": created_revision,
            "patch": {
                "kind": "placement", "action": "move",
                "project_gid": "999", "section_gid": "888",
            },
        })
        assert moved["status"] == "ok" and moved["effect"] == "applied"

        completed = codex_tool(client, thread_id, alpha, "work_update", {
            "api_version": "1", "operation_id": str(uuid4()),
            "work_id": created_work_id, "observed_revision": created_revision,
            "patch": {"completed": True},
        })
        assert completed["status"] == "ok"
        receipt = completed.get("receipt")
        assert isinstance(receipt, dict)
        created_revision = receipt["resulting_revision"]

        reopened = codex_tool(client, thread_id, alpha, "work_update", {
            "api_version": "1", "operation_id": str(uuid4()),
            "work_id": created_work_id, "observed_revision": created_revision,
            "patch": {"completed": False},
        })
        assert reopened["status"] == "ok"
        receipt = reopened.get("receipt")
        assert isinstance(receipt, dict)
        created_revision = receipt["resulting_revision"]

        commented = codex_tool(client, thread_id, alpha, "work_append", {
            "api_version": "1", "operation_id": str(uuid4()),
            "work_id": created_work_id, "observed_revision": created_revision,
            "text": "Phase 1 MCP-only comment",
        })
        assert commented["status"] == "ok" and commented["effect"] == "applied"
        reread = codex_tool(client, thread_id, alpha, "work_get", {
            "api_version": "1", "work_id": created_work_id,
        })
        reread_item = reread.get("item")
        assert isinstance(reread_item, dict)
        created_revision = reread_item["revision"]

        request_id = uuid4()
        sent = codex_tool(client, thread_id, alpha, "agent_message_send", {
            "api_version": "1", "recipient_name": "Agent Beta",
            "message_id": str(request_id), "payload": {"request": "review"},
        })
        assert sent["status"] == "ok"
        message = sent.get("message")
        assert isinstance(message, dict)
        delivery_id = message["delivery_id"]

        pending = codex_tool(client, thread_id, beta, "agent_message_pending", {
            "api_version": "1",
        })
        pending_messages = pending.get("messages")
        assert pending["status"] == "ok" and isinstance(pending_messages, list)
        assert any(item["delivery_id"] == delivery_id for item in pending_messages)

        received = codex_tool(client, thread_id, beta, "agent_message_receive", {
            "api_version": "1", "delivery_id": delivery_id,
        })
        assert received["status"] == "ok" and received["state"] == "RECEIVED"

        recovered = codex_tool(client, thread_id, replacement, "agent_message_recover", {
            "api_version": "1", "delivery_id": delivery_id,
        })
        assert recovered["status"] == "ok" and recovered["state"] == "RECEIVED"

        result_id = uuid4()
        replied = codex_tool(client, thread_id, replacement, "agent_message_result_send", {
            "api_version": "1", "delivery_id": delivery_id,
            "message_id": str(result_id), "payload": {"result": "pass"},
        })
        assert replied["status"] == "ok"
        disposition = codex_tool(
            client, thread_id, replacement, "agent_message_disposition",
            {
                "api_version": "1", "delivery_id": delivery_id,
                "result_message_id": str(result_id),
            },
        )
        assert disposition["status"] == "ok" and disposition["state"] == "DISPOSITIONED"

        returned = codex_tool(client, thread_id, alpha, "agent_message_pending", {
            "api_version": "1",
        })
        returned_messages = returned.get("messages")
        assert returned["status"] == "ok" and isinstance(returned_messages, list)
        assert any(item["message_id"] == str(result_id) for item in returned_messages)

        required = codex_tool(client, thread_id, alpha, "required_result_save", {
            "api_version": "1", "work_id": created_work_id,
            "observed_revision": created_revision,
            "text": "Phase 1 integrated required result",
        })
        assert required["status"] == "ok" and required["effect"] == "applied"

        final_read = codex_tool(client, thread_id, alpha, "work_get", {
            "api_version": "1", "work_id": created_work_id,
        })
        final_item = final_read.get("item")
        assert final_read["status"] == "ok" and isinstance(final_item, dict)
        assert final_item["completed"] is False
    finally:
        client.close()


class JourneyProvider(Provider):
    def __init__(self) -> None:
        super().__init__()
        self.tasks: dict[str, dict[str, object]] = {
            "123": {"title": "MCP Owner", "notes": "", "completed": False, "revision": "r1"},
            "456": {"title": "Execution", "notes": "", "completed": False, "revision": "r1"},
            "789": {"title": "Agent Beta Work", "notes": "", "completed": False, "revision": "r1"},
        }
        self.task_stories: dict[str, list[ProviderSourceStory]] = {
            "123": [ProviderSourceStory(
                "seed-1", "123", "comment_added", "seed history",
                "2026-09-27T00:00:00Z", "proof",
            )],
        }
        self.created: dict[UUID, str] = {}
        self.relations: dict[str, ProviderRelation] = {}
        self.create_count = 0
        self.story_count = 1

    @staticmethod
    def _revision_number(revision: str) -> int:
        return int(revision.removeprefix("r"))

    def _bump(self, task_gid: str) -> str:
        task = self.tasks[task_gid]
        revision = f"r{self._revision_number(str(task['revision'])) + 1}"
        task["revision"] = revision
        return revision

    def recovery_identity(self) -> str:
        return "phase1-proof-provider-v1"

    async def get(self, task_gid: str) -> ProviderWork | None:
        task = self.tasks.get(task_gid)
        if task is None:
            return None
        return ProviderWork(
            str(task["title"]), str(task["notes"]), bool(task["completed"]),
            str(task["revision"]), Routing(priority="P0"), WorkContext(), True,
        )

    async def source_task(self, task_gid: str) -> ProviderSourceTask | None:
        if task_gid == REGISTRY_TASK_GID:
            return ProviderSourceTask(
                "Registry",
                'MCP_RESOLVER_V1\n{"chatgpt_mcp":{"owner":"123","roles":'
                '{"execution":["456"],"spec":["789"]}}}',
                False, "registry-r1", True,
            )
        task = self.tasks.get(task_gid)
        if task is None:
            return None
        return ProviderSourceTask(
            str(task["title"]), str(task["notes"]), bool(task["completed"]),
            str(task["revision"]), True,
        )

    async def source_stories(
        self, task_gid: str, revision: str, offset: str | None, limit: int,
    ) -> ProviderStoriesPage | None:
        task = self.tasks.get(task_gid)
        if task is None:
            return None
        current = str(task["revision"])
        stories = self.task_stories.get(task_gid, [])
        if revision != current:
            return ProviderStoriesPage(task_gid, current, (), None, True, stale=True)
        start = int(offset or 0)
        end = start + limit
        return ProviderStoriesPage(
            task_gid, current, tuple(stories[start:end]),
            str(end) if end < len(stories) else None, True,
        )

    async def source_story(
        self, task_gid: str, story_gid: str,
    ) -> ProviderSourceStory | None:
        return next(
            (story for story in self.task_stories.get(task_gid, [])
             if story.story_gid == story_gid),
            None,
        )

    async def update(self, task_gid: str, patch) -> None:
        task = self.tasks[task_gid]
        for field in patch.model_fields_set:
            if field in {"title", "notes", "completed"}:
                task[field] = getattr(patch, field)
        self._bump(task_gid)

    async def append(self, task_gid: str, text: str) -> str:
        self.story_count += 1
        story_gid = f"story-{self.story_count}"
        self.task_stories.setdefault(task_gid, []).append(ProviderSourceStory(
            story_gid, task_gid, "comment_added", text,
            "2026-09-27T00:00:00Z", "proof",
        ))
        self._bump(task_gid)
        return story_gid

    async def create_work(
        self, title: str, notes: str, operation_id: UUID, *,
        parent_task_gid: str | None = None, project_gid: str | None = None,
    ) -> str:
        if parent_task_gid is None and project_gid != "999":
            raise ProviderError("project denied")
        if parent_task_gid is not None and parent_task_gid not in self.tasks:
            raise ProviderError("parent denied")
        self.create_count += 1
        task_gid = str(9000 + self.create_count)
        self.tasks[task_gid] = {
            "title": title, "notes": notes, "completed": False, "revision": "r1",
        }
        self.created[operation_id] = task_gid
        return task_gid

    async def recover_created(
        self, parent_task_gid: str | None, operation_id: UUID, *,
        project_gid: str | None = None,
    ) -> str | None:
        del parent_task_gid, project_gid
        return self.created.get(operation_id)

    async def update_relation(self, task_gid: str, patch: ProviderRelation) -> None:
        if task_gid not in self.tasks:
            raise ProviderError("relation target missing")
        self.relations[task_gid] = patch

    async def relation_matches(self, task_gid: str, patch: ProviderRelation) -> bool:
        return self.relations.get(task_gid) == patch


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
        assert isinstance(tools, dict) and "grant_get" in tools, entry

        thread = response_result(client.request(
            "thread/start", {"cwd": str(workspace), "ephemeral": True}
        ))
        thread_data = thread.get("thread")
        assert isinstance(thread_data, dict)
        thread_id = thread_data.get("id")
        assert isinstance(thread_id, str)

        for _ in range(2):
            called = response_result(client.request(
                "mcpServer/tool/call",
                {
                    "threadId": thread_id,
                    "server": SERVER_NAME,
                    "tool": "grant_get",
                    "arguments": {"api_version": "1"},
                },
            ))
            structured = called.get("structuredContent")
            assert isinstance(structured, dict), called
            assert structured["status"] == "ok"
            principal = structured["principal"]
            assert isinstance(principal, dict)
            assert principal["issuer"] == ISSUER
            assert principal["subject"] == GITHUB_ID
            assert principal["client_id"] == "codex-proof"
    finally:
        client.close()


@pytest.mark.asyncio
async def test_current_codex_app_server_calls_grant_get_over_stateful_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    raw_binary = os.getenv("CODEX_EXEC_PATH")
    if not raw_binary:
        pytest.skip("real Codex binary is supplied only by the Stage-5 proof job")
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
    subject = service()
    subject.grants.grant = grant(
        principal=PrincipalContext(
            issuer=ISSUER,
            subject=GITHUB_ID,
            client_id="codex-proof",
            assurance="authenticated",
        ),
        scope="workspace",
        operations=frozenset({"work_get"}),
    )
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
            'enabled_tools = ["grant_get"]\n'
        )
        env = dict(os.environ)
        env["CODEX_HOME"] = str(codex_home)
        env["SWITCHSTAND_PROOF_TOKEN"] = "proof-token"
        await asyncio.to_thread(codex_smoke, binary, workspace, env)
    finally:
        server.should_exit = True
        await server_task


@pytest.mark.asyncio
async def test_current_codex_app_server_runs_phase1_mcp_only_journey(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    raw_binary = os.getenv("CODEX_EXEC_PATH")
    url = os.getenv("TEST_DATABASE_URL")
    if not raw_binary or not url:
        pytest.skip("real Codex binary and TEST_DATABASE_URL are supplied by the Stage-5 proof job")
    binary = Path(raw_binary)
    assert binary.is_file() and os.access(binary, os.X_OK)

    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.run_sync(metadata.create_all)
    state, grants = PostgresState(engine), GrantState(engine)
    provider = JourneyProvider()
    alpha_handle = await state.bind("asana", "123")
    beta_handle = await state.bind("asana", "789")
    reference_handle = await state.bind("asana", "456")

    alpha = PrincipalContext(
        issuer=ISSUER, subject=GITHUB_ID, client_id="codex-alpha", assurance="authenticated"
    )
    beta = PrincipalContext(
        issuer=ISSUER, subject=GITHUB_ID, client_id="codex-beta", assurance="authenticated"
    )
    expires = datetime.now(UTC) + timedelta(hours=1)
    alpha_grant = WorkGrant(
        id=uuid4(), version=1, principal=alpha,
        authority=LaunchAuthority(
            active_work_id=alpha_handle.id,
            reference_work_ids=(reference_handle.id,),
        ),
        scope="workspace",
        operations=frozenset({
            "work_get", "work_search", "work_append", "work_create", "work_update", "message"
        }),
        issuer="phase1-proof", provenance="real Codex Phase 1 proof",
        expires_at=expires,
        append_qualification="real:phase1-proof",
        create_qualification="real:phase1-proof",
        update_qualification="real:phase1-proof",
    )
    beta_grant = WorkGrant(
        id=uuid4(), version=1, principal=beta,
        authority=LaunchAuthority(active_work_id=beta_handle.id),
        scope="workspace", operations=frozenset({"work_get", "message"}),
        issuer="phase1-proof", provenance="real Codex Phase 1 proof",
        expires_at=expires,
    )
    await grants.issue(alpha_grant, None)
    await grants.issue(beta_grant, None)

    async def unresolved() -> None:
        return None

    service = ChatGPTService(
        unresolved, state, grants, {"asana": provider},
        MessageState(engine, grants),
        RequiredResultPersistence(LifecycleRepository(engine)),
    )

    tokens = {
        "alpha-token": ("codex-alpha", alpha),
        "beta-token": ("codex-beta", beta),
        "beta-replacement-token": ("codex-beta", beta),
    }

    async def verified(_self, token: str) -> AccessToken | None:
        selected = tokens.get(token)
        if selected is None:
            return None
        client_id, principal = selected
        return AccessToken(
            token=token, client_id=client_id, scopes=[REQUIRED_SCOPE],
            subject=principal.subject, claims={"iss": principal.issuer},
            resource=RESOURCE, expires_at=int(time.time()) + 300,
        )

    monkeypatch.setattr(SwitchstandGitHubProvider, "verify_token", verified)
    app = create_app(service, CONFIG, client_storage=MemoryStore())

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

        codex_home = tmp_path / "phase1-codex-home"
        workspace = tmp_path / "phase1-workspace"
        codex_home.mkdir()
        workspace.mkdir()
        config_lines = ['approval_policy = "never"']
        for name, token_env in (
            ("switchstand_alpha", "SWITCHSTAND_ALPHA_TOKEN"),
            ("switchstand_beta", "SWITCHSTAND_BETA_TOKEN"),
            ("switchstand_beta_replacement", "SWITCHSTAND_BETA_REPLACEMENT_TOKEN"),
        ):
            config_lines.extend([
                f"[mcp_servers.{name}]",
                f'url = "http://127.0.0.1:{port}/mcp"',
                f'bearer_token_env_var = "{token_env}"',
                "required = true",
                'default_tools_approval_mode = "approve"',
            ])
        (codex_home / "config.toml").write_text("\n".join(config_lines) + "\n")
        env = dict(os.environ)
        env["CODEX_HOME"] = str(codex_home)
        env["SWITCHSTAND_ALPHA_TOKEN"] = "alpha-token"
        env["SWITCHSTAND_BETA_TOKEN"] = "beta-token"
        env["SWITCHSTAND_BETA_REPLACEMENT_TOKEN"] = "beta-replacement-token"
        await asyncio.to_thread(
            codex_full_journey, binary, workspace, env, alpha_handle.id, beta_handle.id
        )
    finally:
        server.should_exit = True
        await server_task
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()
