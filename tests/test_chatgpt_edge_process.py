"""Real process/socket/PostgreSQL replay; token verification and provider are fixtures."""

import asyncio
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from chatgpt_fixture import Provider, assert_public, grant, read_chain
from disposable_postgres import (
    clean_environment,
    exited,
    free_port,
    owned_process,
    private_directory,
)
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AccessToken
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.contracts import LaunchAuthority
from switchstand.core import ProviderSourceStory, UnknownEffect
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext
from switchstand.managed_identity import rotate_managed_grant
from switchstand.state import PostgresState
from switchstand.workspace_admission import WorkspaceAdmissionState

TOOLS = {
    "repository_bundle_get", "repository_candidate_qualification_get", "agent_project_bootstrap",
    "work_get", "work_search", "work_resolve_reference",
    "work_history", "work_event", "work_append",
    "work_create", "work_update", "work_relate", "effect_reconcile",
    "agent_register", "agent_takeover", "agent_message_send", "agent_message_pending",
    "agent_message_receive", "agent_message_recover",
    "agent_message_result_send", "agent_message_disposition",
    "required_result_save",
}
ISSUER = "https://switchstand.example/"
RESOURCE = ISSUER + "mcp"
CLIENT_ID = "chatgpt-client"


class CountingProvider(Provider):
    """Persist only the synthetic provider's stories across edge restarts."""

    def __init__(self):
        super().__init__()
        self.path = Path(os.environ["EFFECT_FILE"])
        self.state_path = Path(os.environ["PROVIDER_STATE_FILE"])
        if self.path.exists():
            self.stories = [ProviderSourceStory(**row) for row in json.loads(self.path.read_text())]
        self.sends = len(self.stories)
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            self.title, self.notes = state["title"], state["notes"]
            self.completed, self.revision = state["completed"], state["revision"]
        else:
            self.revision = f"r{self.sends + 1}"

    def _count(self, operation):
        path = Path(os.environ["PROVIDER_CALL_FILE"])
        counts = json.loads(path.read_text()) if path.exists() else {}
        counts[operation] = counts.get(operation, 0) + 1
        path.write_text(json.dumps(counts))

    async def get(self, task_gid):
        self._count("get")
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            self.title, self.notes = state["title"], state["notes"]
            self.completed, self.revision = state["completed"], state["revision"]
        return await super().get(task_gid)

    async def list_attachments(self, task_gid, cursor, limit):
        self._count("list_attachments")
        return await super().list_attachments(task_gid, cursor, limit)

    async def source_stories(self, task_gid, revision, offset, limit, *, require_canonical=True):
        if not self.stories:
            from switchstand.core import ProviderStoriesPage
            story = ProviderSourceStory("raw-read-event", task_gid, "comment_added", "history", "now", "Marco")
            return ProviderStoriesPage(task_gid, self.revision,
                                       (story,) if revision == self.revision else (), None, True,
                                       stale=revision != self.revision)
        return await super().source_stories(
            task_gid, revision, offset, limit, require_canonical=require_canonical
        )

    async def source_story(self, task_gid, story_gid):
        if story_gid == "raw-read-event":
            return ProviderSourceStory(story_gid, task_gid, "comment_added", "history", "now", "Marco")
        return await super().source_story(task_gid, story_gid)

    async def append(self, task_gid, text):
        from dataclasses import asdict
        story = await super().append(task_gid, text)
        self.path.write_text(json.dumps([asdict(row) for row in self.stories]))
        if text == "injected lost response":
            raise UnknownEffect("synthetic response lost after durable provider effect")
        return story

    async def update(self, task_gid, patch):
        self._count("update")
        await super().update(task_gid, patch)
        self.state_path.write_text(json.dumps({
            "title": self.title, "notes": self.notes,
            "completed": self.completed, "revision": self.revision,
        }))


def _child_server() -> None:
    import switchstand.chatgpt_edge as edge

    async def verified(_self, token):
        return AccessToken(
            token=token, client_id=CLIENT_ID, scopes=[edge.REQUIRED_SCOPE],
            subject=os.environ["SWITCHSTAND_MCP_GITHUB_USER_ID"],
            claims={"iss": ISSUER}, resource=RESOURCE, expires_at=int(time.time()) + 300,
        )

    edge.SwitchstandGitHubProvider.verify_token = verified
    edge.AsanaProvider = lambda _client, _project=None, **_kwargs: CountingProvider()
    edge.create_app = partial(edge.create_app, client_storage=MemoryStore())
    original_get = edge.ChatGPTService.get

    async def delayed_get(service, work_id=None):
        marker = os.getenv("SLOW_GET_STARTED_FILE")
        if marker:
            Path(marker).write_text("started\n")
            await asyncio.sleep(float(os.environ["SLOW_GET_SECONDS"]))
        return await original_get(service, work_id)

    edge.ChatGPTService.get = delayed_get
    edge.main()


def _managed_server() -> None:
    import switchstand.mcp as managed

    managed.AsanaProvider = lambda _client, _project=None: CountingProvider()
    managed.main()


async def _provision(url, subject):
    engine = create_async_engine(url)
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    os.environ["DATABASE_URL"] = url
    command.upgrade(config, "head")
    state, grants = PostgresState(engine), GrantState(engine)
    active = await state.bind("asana", "123")
    reference = await state.bind("asana", "456")
    denied = await state.bind("asana", "789")
    principal = PrincipalContext(
        issuer=ISSUER, subject=subject, client_id=CLIENT_ID, assurance="authenticated",
    )
    selected = grant(
        principal=principal, active=active.id, reference=reference.id,
        scope="workspace",
        operations=frozenset({"work_get", "work_search"}),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        append_qualification=None,
    )
    await grants.issue(selected, None)
    await engine.dispose()
    return selected, denied.id


async def _replace_with_launch(url, selected):
    engine = create_async_engine(url)
    launched = selected.model_copy(update={
        "id": uuid4(), "version": 2, "scope": "launch",
        "operations": frozenset({"work_get", "work_append"}),
        "append_qualification": "real:disposable-switchstand-test",
    })
    await GrantState(engine).issue(launched, 1)
    await engine.dispose()
    return launched


async def _provision_composed(url, subject):
    selected, denied = await _provision(url, subject)
    engine = create_async_engine(url)
    grants = GrantState(engine)
    selected = selected.model_copy(update={
        "id": uuid4(), "version": 2, "scope": "workspace",
        "operations": frozenset({"work_get", "message"}),
    })
    await grants.issue(selected, 1)
    recipient = selected.authority.reference_work_ids[0]
    managed = await rotate_managed_grant(grants, LaunchAuthority(active_work_id=recipient))
    await engine.dispose()
    return selected, managed, denied


@contextmanager
def _server(env, port):
    run = Path(env["EFFECT_FILE"]).parent
    with owned_process([sys.executable, __file__, "--serve"], env,
                       run / f"edge-{uuid4()}.log", new_session=False) as process:
        yield _ready_server(process, port)


def _ready_server(process, port):
    endpoint = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    with httpx.Client(trust_env=False, timeout=0.2) as client:
        while time.monotonic() < deadline:
            if exited(process) is not None:
                raise AssertionError(f"edge process exited {process.returncode}")
            try:
                response = client.get(endpoint + "/.well-known/oauth-protected-resource/mcp")
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("edge process did not start")
    return endpoint


async def _discover(endpoint, selected):
    transport = StreamableHttpTransport(endpoint + "/mcp", auth="fixed-bearer")
    async with Client(transport) as client:
        listed_tools = await client.list_tools()
        assert {tool.name for tool in listed_tools} == TOOLS
        for tool in listed_tools:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is (
                tool.name != "agent_project_bootstrap"
            )
            assert tool.annotations.open_world_hint is False
        observed = (await client.call_tool("work_get", {
            "api_version": "1", "work_id": str(selected.authority.active_work_id),
        })).structured_content
        assert observed["item"]["id"] == str(selected.authority.active_work_id)
        search = (await client.call_tool("work_search", {
            "api_version": "1", "text": "Task", "limit": 10,
        })).structured_content
        assert search["status"] == "ok" and len(search["items"]) == 1
        for tool in listed_tools:
            if tool.name in {"work_search", "work_get", "work_history", "work_event"}:
                assert_public(tool.model_dump(mode="json"))
                assert tool.inputSchema.get("additionalProperties") is False
        await read_chain(client, search["items"][0]["id"], exceptional_purpose=True)
        assert "provider" not in search["items"][0] and "task_gid" not in search["items"][0]


async def _exercise(endpoint, selected, operation_id):
    transport = StreamableHttpTransport(endpoint + "/mcp", auth="fixed-bearer")
    async with Client(transport) as client:
        result = await client.call_tool("work_append", {
            "api_version": "1", "operation_id": str(operation_id),
            "work_id": str(selected.authority.active_work_id),
            "observed_revision": "r1", "text": "durable vertical append",
            "purpose": "provenance",
        })
        return result.structured_content


async def _agent_call(endpoint, chat_session, tool, arguments):
    transport = StreamableHttpTransport(endpoint + "/mcp", auth="fixed-bearer")
    async with Client(transport) as client:
        result = await client.call_tool(
            tool, arguments,
            meta={} if chat_session is None else {"openai/session": chat_session},
        )
        return result.structured_content


async def test_sigterm_drains_an_inflight_mcp_call(
    tmp_path: Path, database_prerequisite,
):
    url = os.environ["TEST_DATABASE_URL"]
    subject = str(uuid4().int)
    selected, _ = await _provision(url, subject)
    port = free_port()
    marker = tmp_path / "slow-get-started"
    env = clean_environment() | {
        "DATABASE_URL": url,
        "ASANA_TOKEN": "test-only",
        "EFFECT_FILE": str(tmp_path / "effects"),
        "PROVIDER_STATE_FILE": str(tmp_path / "provider-state.json"),
        "PROVIDER_CALL_FILE": str(tmp_path / "provider-calls.json"),
        "SWITCHSTAND_MCP_GITHUB_CLIENT_ID": "fixture",
        "SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET": "fixture",
        "SWITCHSTAND_MCP_GITHUB_USER_ID": subject,
        "SWITCHSTAND_MCP_RESOURCE_URL": RESOURCE,
        "SWITCHSTAND_MCP_BIND_HOST": "127.0.0.1",
        "SWITCHSTAND_MCP_BIND_PORT": str(port),
        "SLOW_GET_STARTED_FILE": str(marker),
        "SLOW_GET_SECONDS": "3",
    }
    log = tmp_path / "edge.log"
    with owned_process(
        [sys.executable, __file__, "--serve"], env, log,
        new_session=False, termination_grace=6,
    ) as process:
        endpoint = _ready_server(process, port)
        base_headers = {
            "accept": "application/json, text/event-stream",
            "authorization": "Bearer fixed-bearer",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(base_url=endpoint, trust_env=False, timeout=5) as raw:
            initialized = await raw.post("/mcp", headers=base_headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "clientInfo": {"name": "shutdown-proof", "version": "1"},
                    "protocolVersion": "2025-03-26", "capabilities": {},
                },
            })
            assert initialized.status_code == 200
            session_headers = base_headers | {
                "mcp-session-id": initialized.headers["mcp-session-id"],
                "mcp-protocol-version": "2025-03-26",
            }
            ready = await raw.post("/mcp", headers=session_headers, json={
                "jsonrpc": "2.0", "method": "notifications/initialized",
            })
            assert ready.status_code == 202
            call = asyncio.create_task(raw.post("/mcp", headers=session_headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                    "name": "work_get", "arguments": {
                        "api_version": "1",
                        "work_id": str(selected.authority.active_work_id),
                    },
                },
            }))
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert marker.exists(), "MCP call did not enter the delayed handler"

            process.terminate()

            response = await call
            assert response.status_code == 200
            assert response.json()["result"]["structuredContent"]["status"] == "ok"
            exit_deadline = time.monotonic() + 2
            while exited(process) is None and time.monotonic() < exit_deadline:
                await asyncio.sleep(0.02)
            assert exited(process) is not None, "edge did not exit after draining the request"
    assert "ASGI callable returned without completing response" not in log.read_text()


async def test_sigterm_closes_a_persistent_mcp_stream(
    tmp_path: Path, database_prerequisite,
):
    url = os.environ["TEST_DATABASE_URL"]
    subject = str(uuid4().int)
    await _provision(url, subject)
    port = free_port()
    env = clean_environment() | {
        "DATABASE_URL": url,
        "ASANA_TOKEN": "test-only",
        "EFFECT_FILE": str(tmp_path / "effects"),
        "PROVIDER_STATE_FILE": str(tmp_path / "provider-state.json"),
        "PROVIDER_CALL_FILE": str(tmp_path / "provider-calls.json"),
        "SWITCHSTAND_MCP_GITHUB_CLIENT_ID": "fixture",
        "SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET": "fixture",
        "SWITCHSTAND_MCP_GITHUB_USER_ID": subject,
        "SWITCHSTAND_MCP_RESOURCE_URL": RESOURCE,
        "SWITCHSTAND_MCP_BIND_HOST": "127.0.0.1",
        "SWITCHSTAND_MCP_BIND_PORT": str(port),
    }
    log = tmp_path / "edge.log"
    with owned_process(
        [sys.executable, __file__, "--serve"], env, log,
        new_session=False, termination_grace=6,
    ) as process:
        endpoint = _ready_server(process, port)
        base_headers = {
            "accept": "application/json, text/event-stream",
            "authorization": "Bearer fixed-bearer",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(base_url=endpoint, trust_env=False, timeout=None) as raw:
            initialized = await raw.post("/mcp", headers=base_headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "clientInfo": {"name": "shutdown-proof", "version": "1"},
                    "protocolVersion": "2025-03-26", "capabilities": {},
                },
            })
            assert initialized.status_code == 200
            session_headers = base_headers | {
                "mcp-session-id": initialized.headers["mcp-session-id"],
                "mcp-protocol-version": "2025-03-26",
            }
            ready = await raw.post("/mcp", headers=session_headers, json={
                "jsonrpc": "2.0", "method": "notifications/initialized",
            })
            assert ready.status_code == 202
            async with raw.stream("GET", "/mcp", headers=session_headers) as stream:
                assert stream.status_code == 200
                assert stream.headers["content-type"].startswith("text/event-stream")
                process.terminate()
                exit_deadline = time.monotonic() + 2
                while exited(process) is None and time.monotonic() < exit_deadline:
                    await asyncio.sleep(0.02)
                assert exited(process) is not None, "edge did not close the persistent stream"
                assert await stream.aread() == b""
    assert "ASGI callable returned without completing response" not in log.read_text()

    restart_log = tmp_path / "restarted-edge.log"
    with owned_process(
        [sys.executable, __file__, "--serve"], env, restart_log, new_session=False,
    ) as restarted:
        endpoint = _ready_server(restarted, port)
        async with httpx.AsyncClient(base_url=endpoint, trust_env=False, timeout=5) as raw:
            expired = await raw.get("/mcp", headers=session_headers)
            assert expired.status_code == 404
            reinitialized = await raw.post("/mcp", headers=base_headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {
                    "clientInfo": {"name": "shutdown-proof", "version": "1"},
                    "protocolVersion": "2025-03-26", "capabilities": {},
                },
            })
            assert reinitialized.status_code == 200
            assert reinitialized.headers["mcp-session-id"] != session_headers["mcp-session-id"]


async def test_agent_identity_survives_http_transport_and_process_churn(
    tmp_path: Path, database_prerequisite,
):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for the MCP process test")
    subject = str(uuid4().int)
    await _provision(url, subject)
    port = free_port()
    env = clean_environment() | {
        "DATABASE_URL": url, "ASANA_TOKEN": "test-only",
        "EFFECT_FILE": str(tmp_path / "effects"),
        "PROVIDER_STATE_FILE": str(tmp_path / "provider-state.json"),
        "PROVIDER_CALL_FILE": str(tmp_path / "provider-calls.json"),
        "SWITCHSTAND_MCP_GITHUB_CLIENT_ID": "fixture",
        "SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET": "fixture",
        "SWITCHSTAND_MCP_GITHUB_USER_ID": subject,
        "SWITCHSTAND_MCP_RESOURCE_URL": RESOURCE,
        "SWITCHSTAND_MCP_BIND_HOST": "127.0.0.1",
        "SWITCHSTAND_MCP_BIND_PORT": str(port),
    }
    register = lambda name: {"api_version": "1", "name": name}
    suffix = str(uuid4())
    alpha, beta = f"Alpha {suffix}", f"Beta {suffix}"
    with _server(env, port) as endpoint:
        assert (await _agent_call(endpoint, "chat-a", "agent_register", register(alpha)))[
            "status"
        ] == "ok"
        assert (await _agent_call(endpoint, "chat-b", "agent_register", register(beta)))[
            "status"
        ] == "ok"
        missing = await _agent_call(endpoint, None, "agent_register", register("Missing"))
        assert (missing["status"], missing["reason"]) == (
            "recovery_required", "runtime_identity_unavailable",
        )
        sent = await _agent_call(endpoint, "chat-a", "agent_message_send", {
            "api_version": "1", "recipient_name": beta,
            "message_id": str(uuid4()), "payload": {"request": "review"},
        })
        assert "bounded wait" in sent["next_action"]
        delivery = sent["message"]["delivery_id"]

    # Every operation below uses a fresh HTTP/MCP transport after a server restart.
    with _server(env, port) as endpoint:
        pending = await _agent_call(endpoint, "chat-b", "agent_message_pending", {
            "api_version": "1",
        })
        assert pending["messages"][0]["delivery_id"] == delivery
        received = await _agent_call(endpoint, "chat-b", "agent_message_receive", {
            "api_version": "1", "delivery_id": delivery,
        })
        assert received["state"] == "RECEIVED"
        result_id = str(uuid4())
        replied = await _agent_call(endpoint, "chat-b", "agent_message_result_send", {
            "api_version": "1", "delivery_id": delivery,
            "message_id": result_id, "payload": {"result": "pass"},
        })
        assert replied["status"] == "ok"
        assert replied.get("next_action") is None
        disposed = await _agent_call(endpoint, "chat-b", "agent_message_disposition", {
            "api_version": "1", "delivery_id": delivery, "result_message_id": result_id,
        })
        assert disposed["state"] == "DISPOSITIONED"
        returned = await _agent_call(endpoint, "chat-a", "agent_message_pending", {
            "api_version": "1",
        })
        assert returned["messages"][0]["message_id"] == result_id

        self_sent = await _agent_call(endpoint, "chat-a", "agent_message_send", {
            "api_version": "1", "recipient_name": alpha,
            "message_id": str(uuid4()), "payload": {"request": "self-review"},
        })
        self_delivery = self_sent["message"]["delivery_id"]
        assert (await _agent_call(endpoint, "chat-a", "agent_message_receive", {
            "api_version": "1", "delivery_id": self_delivery,
        }))["state"] == "RECEIVED"
        assert (await _agent_call(endpoint, "chat-a", "agent_message_recover", {
            "api_version": "1", "delivery_id": self_delivery,
        }))["state"] == "RECEIVED"
        self_result_id = str(uuid4())
        self_reply = await _agent_call(endpoint, "chat-a", "agent_message_result_send", {
            "api_version": "1", "delivery_id": self_delivery,
            "message_id": self_result_id, "payload": {"result": "self-pass"},
        })
        assert self_reply["status"] == "ok"
        self_disposed = await _agent_call(endpoint, "chat-a", "agent_message_disposition", {
            "api_version": "1", "delivery_id": self_delivery,
            "result_message_id": self_result_id,
        })
        assert self_disposed["state"] == "DISPOSITIONED"


async def test_process_with_fixture_identity_replays_durable_append_after_restart(
    database_prerequisite,
):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for the MCP process test")
    assert make_url(url).database == "switchstand_test"
    subject = str(uuid4().int)
    selected, denied = await _provision(url, subject)
    operation_id, port = uuid4(), free_port()
    fallback = Path.home() / ".local/state/switchstand/qualification"
    if not os.getenv("QUALIFICATION_DIRECTORY"):
        fallback.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    run = Path(os.getenv("QUALIFICATION_DIRECTORY", fallback))
    private_directory(run)
    run = run / str(uuid4())
    run.mkdir(mode=0o700)
    effects = run / "effects"
    provider_calls = run / "provider-calls.json"
    env = clean_environment() | {
        "DATABASE_URL": url, "ASANA_TOKEN": "test-only", "EFFECT_FILE": str(effects),
        "PROVIDER_STATE_FILE": str(run / "provider-state.json"),
        "PROVIDER_CALL_FILE": str(provider_calls),
        "SWITCHSTAND_MCP_GITHUB_CLIENT_ID": "fixture", "SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET": "fixture",
        "SWITCHSTAND_MCP_GITHUB_USER_ID": subject, "SWITCHSTAND_MCP_RESOURCE_URL": RESOURCE,
        "SWITCHSTAND_MCP_BIND_HOST": "127.0.0.1", "SWITCHSTAND_MCP_BIND_PORT": str(port),
    }
    with _server(env, port) as endpoint:
        await _discover(endpoint, selected)
    selected = await _replace_with_launch(url, selected)
    with _server(env, port) as endpoint:
        first = await _exercise(endpoint, selected, operation_id)
        assert first["status"] == "ok" and first["effect"] == "applied"
        assert first["receipt"]["operation_id"] == str(operation_id)
        assert first["receipt"]["work_id"] == str(selected.authority.active_work_id)
        admission = WorkspaceAdmissionState.admission(selected.principal)
        assert first["receipt"]["grant_id"] == str(admission.id)
        assert first["receipt"]["grant_version"] == admission.version == 1
        assert await _exercise(endpoint, selected, operation_id) == first
    with _server(env, port) as endpoint:
        assert await _exercise(endpoint, selected, operation_id) == first
        assert len(json.loads(effects.read_text())) == 1
        await _boundaries(endpoint, selected, denied, effects)
    with _server(env, port) as endpoint:
        await _contained_after_restart(endpoint, selected, effects)

async def _boundaries(endpoint, selected, denied_work, effects):
    async with Client(StreamableHttpTransport(endpoint + "/mcp", auth="fixed-bearer")) as client:
        async def call(tool, **args):
            return (await client.call_tool(tool, {"api_version": "1", **args})).structured_content

        active = str(selected.authority.active_work_id)
        args = {
            "work_id": active, "observed_revision": "r2", "text": "second",
            "purpose": "provenance",
        }
        for target in [str(denied_work), str(uuid4())]:
            denied = await call("work_append", **(args | {"work_id": target}),
                                operation_id=str(uuid4()))
            assert denied["status"] == "denied" and denied["effect"] == "not_sent"
        assert len(json.loads(effects.read_text())) == 1
        second = await call("work_append", **args, operation_id=str(uuid4()))
        assert second["status"] == "ok"
        receipt = second["receipt"]
        first = await call(
            "work_history", work_id=active, observed_revision="r3", limit=1,
            purpose="investigation",
        )
        assert len(first["events"]) == 1 and first["next_cursor"] is not None
        last = await call(
            "work_history", work_id=active, observed_revision="r3", limit=1,
            cursor=first["next_cursor"], purpose="investigation",
        )
        assert len(last["events"]) == 1 and last["next_cursor"] is None
        assert first["events"][0]["id"] != last["events"][0]["id"]
        appended = next(
            event for event in (*first["events"], *last["events"])
            if event["text"] == receipt["text"]
        )
        readback = await call(
            "work_event", work_id=active, event_id=appended["id"],
            observed_revision="r3", purpose="investigation",
        )
        assert readback["item"]["text"] == receipt["text"]
        assert readback["item"]["id"] == appended["id"]
        lost_id = str(uuid4())
        lost_args = args | {"observed_revision": "r3", "text": "injected lost response",
                            "operation_id": lost_id}
        lost = await call("work_append", **lost_args)
        assert lost["effect"] == "unknown"
        assert (await call("work_append", **lost_args))["effect"] == "unknown"
        assert len(json.loads(effects.read_text())) == 3
        (effects.parent / "lost.json").write_text(json.dumps(lost_args))


async def _contained_after_restart(endpoint, selected, effects):
    args = json.loads((effects.parent / "lost.json").read_text())
    async with Client(StreamableHttpTransport(endpoint + "/mcp", auth="fixed-bearer")) as client:
        for request in [args, args | {"operation_id": str(uuid4()), "observed_revision": "r4"}]:
            result = (await client.call_tool("work_append", {
                "api_version": "1", **request,
            })).structured_content
            assert result["effect"] in {"unknown", "not_sent"} and result["status"] != "ok"
        assert len(json.loads(effects.read_text())) == 3


if __name__ == "__main__" and sys.argv[1:] == ["--serve"]:
    _child_server()
elif __name__ == "__main__" and sys.argv[1:] == ["--serve-managed"]:
    _managed_server()
