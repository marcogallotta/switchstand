"""Real process/socket/PostgreSQL replay; token verification and provider are fixtures."""

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
from mcp import Client as MCPClient
from mcp import StdioServerParameters
from mcp.server.auth.provider import AccessToken
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.contracts import LaunchAuthority
from switchstand.core import ProviderSourceStory, UnknownEffect
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext
from switchstand.managed_identity import rotate_managed_grant
from switchstand.run import RunReceipt, process_start_token
from switchstand.state import PostgresState

TOOLS = {
    "grant_get", "work_get", "work_search", "work_resolve_reference", "work_structure",
    "source_task", "source_stories",
    "source_story", "work_history", "work_attachments", "work_event", "work_append",
    "work_create", "work_update", "message_send", "message_pending",
    "message_receive", "message_recover", "message_result_send", "message_disposition",
    "required_result_save",
}
ISSUER = "https://switchstand.example/"
RESOURCE = ISSUER + "mcp"
CLIENT_ID = "chatgpt-client"
RECIPIENT_CLIENT_ID = "chatgpt-recipient"


class CountingProvider(Provider):
    """Persist only the synthetic provider's stories across edge restarts."""

    def __init__(self):
        super().__init__()
        self.canonical_ids.update(
            value for value in (
                os.getenv("STAGE5_SENDER_GID"),
                os.getenv("STAGE5_RECIPIENT_GID"),
            ) if value
        )
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

    async def search_work(self, text, completed, cursor, limit):
        page = await super().search_work(text, completed, cursor, limit)
        sender_gid = os.getenv("STAGE5_SENDER_GID")
        if not sender_gid:
            return page
        from dataclasses import replace
        return replace(
            page,
            items=(replace(page.items[0], provider_work_id=sender_gid),),
        )

    async def list_attachments(self, task_gid, cursor, limit):
        self._count("list_attachments")
        return await super().list_attachments(task_gid, cursor, limit)

    async def source_stories(self, task_gid, revision, offset, limit):
        if not self.stories:
            from switchstand.core import ProviderStoriesPage
            story = ProviderSourceStory("raw-read-event", task_gid, "comment_added", "history", "now", "Marco")
            return ProviderStoriesPage(task_gid, self.revision,
                                       (story,) if revision == self.revision else (), None, True,
                                       stale=revision != self.revision)
        return await super().source_stories(task_gid, revision, offset, limit)

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
        client_id = RECIPIENT_CLIENT_ID if token == "recipient-bearer" else CLIENT_ID
        return AccessToken(
            token=token, client_id=client_id, scopes=[edge.REQUIRED_SCOPE],
            subject=os.environ["SWITCHSTAND_MCP_GITHUB_USER_ID"],
            claims={"iss": ISSUER}, resource=RESOURCE, expires_at=int(time.time()) + 300,
        )

    edge.SwitchstandGitHubProvider.verify_token = verified
    edge.AsanaProvider = lambda _client, _project=None, **_kwargs: CountingProvider()
    edge.create_app = partial(edge.create_app, client_storage=MemoryStore())
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
        "id": uuid4(), "version": 2, "scope": "launch",
        "operations": frozenset({"work_get", "message"}),
    })
    await grants.issue(selected, 1)
    recipient = selected.authority.reference_work_ids[0]
    managed = await rotate_managed_grant(grants, LaunchAuthority(active_work_id=recipient))
    await engine.dispose()
    return selected, managed, denied


async def _provision_ordinary_stage5(url, subject, sender_gid, recipient_gid):
    engine = create_async_engine(url)
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    os.environ["DATABASE_URL"] = url
    command.upgrade(config, "head")
    state, grants = PostgresState(engine), GrantState(engine)
    sender = await state.bind("asana", sender_gid)
    recipient = await state.bind("asana", recipient_gid)
    sender_principal = PrincipalContext(
        issuer=ISSUER, subject=subject, client_id=CLIENT_ID, assurance="authenticated",
    )
    recipient_principal = PrincipalContext(
        issuer=ISSUER, subject=subject, client_id=RECIPIENT_CLIENT_ID, assurance="authenticated",
    )
    sender_grant = grant(
        principal=sender_principal,
        active=sender.id,
        reference=recipient.id,
        scope="workspace",
        operations=frozenset({
            "work_get", "work_search", "work_append", "work_update", "message"
        }),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        append_qualification="real:stage5-process",
        update_qualification="real:stage5-process",
    )
    recipient_grant = grant(
        principal=recipient_principal,
        active=recipient.id,
        reference=sender.id,
        scope="launch",
        operations=frozenset({"work_get", "message"}),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        append_qualification=None,
    )
    await grants.issue(sender_grant, None)
    await grants.issue(recipient_grant, None)
    await engine.dispose()
    return sender_grant, recipient_grant


@contextmanager
def _server(env, port):
    run = Path(env["EFFECT_FILE"]).parent
    with owned_process([sys.executable, __file__, "--serve"], env,
                       run / f"edge-{uuid4()}.log", new_session=False) as process:
        yield from _ready_server(process, port)


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
    yield endpoint


async def _discover(endpoint, selected):
    transport = StreamableHttpTransport(endpoint + "/mcp", auth="fixed-bearer")
    async with Client(transport) as client:
        assert {tool.name for tool in await client.list_tools()} == TOOLS
        observed = (await client.call_tool("work_get", {
            "api_version": "1", "work_id": str(selected.authority.active_work_id),
        })).structured_content
        assert observed["item"]["id"] == str(selected.authority.active_work_id)
        search = (await client.call_tool("work_search", {
            "api_version": "1", "text": "Task", "limit": 10,
        })).structured_content
        assert search["status"] == "ok" and len(search["items"]) == 1
        for tool in await client.list_tools():
            if tool.name in {"work_search", "work_get", "work_structure", "work_history", "work_attachments", "work_event"}:
                assert_public(tool.model_dump(mode="json"))
                assert tool.inputSchema.get("additionalProperties") is False
                if tool.name == "work_attachments":
                    schema = tool.inputSchema
                    assert set(schema["required"]) == {"api_version", "work_id", "observed_revision"}
                    assert schema["properties"]["observed_revision"]["minLength"] == 1
                    cursor_types = schema["properties"]["cursor"]["anyOf"]
                    assert next(item for item in cursor_types if item.get("type") == "string")[
                        "maxLength"
                    ] == 1024
                    limit = schema["properties"]["limit"]
                    assert (limit["default"], limit["minimum"], limit["maximum"]) == (50, 1, 100)
        await read_chain(client, search["items"][0]["id"])
        assert "provider" not in search["items"][0] and "task_gid" not in search["items"][0]
        assert (await client.call_tool("grant_get", {"api_version": "1"})).structured_content[
            "grant"
        ]["id"] == str(selected.id)


async def _exercise(endpoint, selected, operation_id):
    transport = StreamableHttpTransport(endpoint + "/mcp", auth="fixed-bearer")
    async with Client(transport) as client:
        result = await client.call_tool("work_append", {
            "api_version": "1", "operation_id": str(operation_id),
            "work_id": str(selected.authority.active_work_id), "grant_version": selected.version,
            "observed_revision": "r1", "text": "durable vertical append",
        })
        return result.structured_content


async def test_process_with_fixture_identity_replays_durable_append_after_restart():
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
        await _attachments(endpoint, selected, denied, provider_calls)
        first = await _exercise(endpoint, selected, operation_id)
        assert first["status"] == "ok" and first["effect"] == "applied"
        assert first["receipt"]["operation_id"] == str(operation_id)
        assert first["receipt"]["work_id"] == str(selected.authority.active_work_id)
        assert first["receipt"]["grant_id"] == str(selected.id)
        assert await _exercise(endpoint, selected, operation_id) == first
    with _server(env, port) as endpoint:
        assert await _exercise(endpoint, selected, operation_id) == first
        assert len(json.loads(effects.read_text())) == 1
        await _boundaries(endpoint, selected, denied, effects)
    with _server(env, port) as endpoint:
        await _contained_after_restart(endpoint, selected, effects)


async def test_ordinary_stateful_review_loop_recovers_same_identities_after_restart(
    tmp_path: Path,
):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for the MCP process test")
    assert make_url(url).database == "switchstand_test"
    subject = str(uuid4().int)
    sender_gid, recipient_gid = str(uuid4().int), str(uuid4().int)
    sender_grant, recipient_grant = await _provision_ordinary_stage5(
        url, subject, sender_gid, recipient_gid
    )
    sender = sender_grant.authority.active_work_id
    recipient = recipient_grant.authority.active_work_id
    request_id, recovery_request_id, result_id = uuid4(), uuid4(), uuid4()
    append_id, update_id = uuid4(), uuid4()
    port = free_port()
    run = tmp_path / str(uuid4())
    run.mkdir(mode=0o700)
    env = clean_environment() | {
        "DATABASE_URL": url,
        "ASANA_TOKEN": "test-only",
        "EFFECT_FILE": str(run / "effects"),
        "PROVIDER_STATE_FILE": str(run / "provider-state.json"),
        "PROVIDER_CALL_FILE": str(run / "provider-calls.json"),
        "SWITCHSTAND_MCP_GITHUB_CLIENT_ID": "fixture",
        "SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET": "fixture",
        "SWITCHSTAND_MCP_GITHUB_USER_ID": subject,
        "SWITCHSTAND_MCP_RESOURCE_URL": RESOURCE,
        "SWITCHSTAND_MCP_BIND_HOST": "127.0.0.1",
        "STAGE5_SENDER_GID": sender_gid,
        "STAGE5_RECIPIENT_GID": recipient_gid,
        "SWITCHSTAND_MCP_BIND_PORT": str(port),
    }

    first_request = {
        "api_version": "1",
        "work_id": str(sender),
        "grant_version": sender_grant.version,
        "message_id": str(request_id),
        "route_ref": "review",
        "recipient_work_id": str(recipient),
        "payload": {"request": "review exact candidate"},
    }
    recovery_request = first_request | {
        "message_id": str(recovery_request_id),
        "payload": {"request": "continue after restart"},
    }

    with _server(env, port) as endpoint:
        async with Client(StreamableHttpTransport(
            endpoint + "/mcp", auth="fixed-bearer"
        )) as sender_client:
            grant_readback = (await sender_client.call_tool(
                "grant_get", {"api_version": "1"}
            )).structured_content
            assert grant_readback["grant"]["id"] == str(sender_grant.id)
            found = (await sender_client.call_tool("work_search", {
                "api_version": "1", "text": "Task", "limit": 10,
            })).structured_content
            assert found["status"] == "ok"
            observed = (await sender_client.call_tool("work_get", {
                "api_version": "1", "work_id": str(sender),
            })).structured_content
            history = (await sender_client.call_tool("work_history", {
                "api_version": "1",
                "work_id": str(sender),
                "observed_revision": observed["item"]["revision"],
                "limit": 10,
            })).structured_content
            assert history["status"] == "ok"

            appended = (await sender_client.call_tool("work_append", {
                "api_version": "1",
                "operation_id": str(append_id),
                "work_id": str(sender),
                "grant_version": sender_grant.version,
                "observed_revision": observed["item"]["revision"],
                "text": "Stage-5 ordinary append",
            })).structured_content
            assert appended["effect"] == "applied"
            after_append = (await sender_client.call_tool("work_get", {
                "api_version": "1", "work_id": str(sender),
            })).structured_content
            updated = (await sender_client.call_tool("work_update", {
                "api_version": "1",
                "operation_id": str(update_id),
                "work_id": str(sender),
                "grant_version": sender_grant.version,
                "observed_revision": after_append["item"]["revision"],
                "patch": {"completed": True},
            })).structured_content
            assert updated["effect"] == "applied"

            sent = (await sender_client.call_tool(
                "message_send", first_request
            )).structured_content
            assert sent["status"] == "ok"
            first_delivery = sent["message"]["delivery_id"]

        async with Client(StreamableHttpTransport(
            endpoint + "/mcp", auth="recipient-bearer"
        )) as recipient_client:
            pending = (await recipient_client.call_tool("message_pending", {
                "api_version": "1",
                "work_id": str(recipient),
                "grant_version": recipient_grant.version,
            })).structured_content
            assert pending["messages"][0]["delivery_id"] == first_delivery
            received = (await recipient_client.call_tool("message_receive", {
                "api_version": "1",
                "work_id": str(recipient),
                "grant_version": recipient_grant.version,
                "delivery_id": first_delivery,
            })).structured_content
            assert received["state"] == "RECEIVED"
            result = (await recipient_client.call_tool("message_result_send", {
                "api_version": "1",
                "work_id": str(recipient),
                "grant_version": recipient_grant.version,
                "in_reply_to_delivery_id": first_delivery,
                "message_id": str(result_id),
                "payload": {"result": "pass"},
            })).structured_content
            assert result["status"] == "ok"
            result_delivery = result["message"]["delivery_id"]
            disposed = (await recipient_client.call_tool("message_disposition", {
                "api_version": "1",
                "work_id": str(recipient),
                "grant_version": recipient_grant.version,
                "delivery_id": first_delivery,
                "result_message_id": str(result_id),
            })).structured_content
            assert disposed["state"] == "DISPOSITIONED"

        async with Client(StreamableHttpTransport(
            endpoint + "/mcp", auth="fixed-bearer"
        )) as sender_client:
            reply = (await sender_client.call_tool("message_pending", {
                "api_version": "1",
                "work_id": str(sender),
                "grant_version": sender_grant.version,
            })).structured_content
            assert reply["messages"][0]["delivery_id"] == result_delivery
            assert reply["messages"][0]["message_id"] == str(result_id)
            current = (await sender_client.call_tool("work_get", {
                "api_version": "1", "work_id": str(sender),
            })).structured_content
            saved = (await sender_client.call_tool("required_result_save", {
                "api_version": "1",
                "work_id": str(sender),
                "grant_version": sender_grant.version,
                "observed_revision": current["item"]["revision"],
                "text": "Stage-5 required result",
            })).structured_content
            assert saved["status"] == "ok"

            second = (await sender_client.call_tool(
                "message_send", recovery_request
            )).structured_content
            second_delivery = second["message"]["delivery_id"]

        async with Client(StreamableHttpTransport(
            endpoint + "/mcp", auth="recipient-bearer"
        )) as recipient_client:
            second_received = (await recipient_client.call_tool("message_receive", {
                "api_version": "1",
                "work_id": str(recipient),
                "grant_version": recipient_grant.version,
                "delivery_id": second_delivery,
            })).structured_content
            assert second_received["state"] == "RECEIVED"

    # Edge restart clears transport sessions/currentness only. Durable message
    # identities survive; a replacement session must explicitly recover.
    with _server(env, port) as endpoint:
        async with Client(StreamableHttpTransport(
            endpoint + "/mcp", auth="recipient-bearer"
        )) as replacement:
            recovered = (await replacement.call_tool("message_recover", {
                "api_version": "1",
                "work_id": str(recipient),
                "grant_version": recipient_grant.version,
                "delivery_id": second_delivery,
            })).structured_content
            assert recovered["status"] == "ok"
            assert recovered["state"] == "RECEIVED"
            pending = (await replacement.call_tool("message_pending", {
                "api_version": "1",
                "work_id": str(recipient),
                "grant_version": recipient_grant.version,
            })).structured_content
            assert pending["messages"][0]["delivery_id"] == second_delivery
            assert pending["messages"][0]["message_id"] == str(recovery_request_id)

        async with Client(StreamableHttpTransport(
            endpoint + "/mcp", auth="fixed-bearer"
        )) as sender_client:
            replay = (await sender_client.call_tool(
                "message_send", recovery_request
            )).structured_content
            assert replay["message"]["delivery_id"] == second_delivery
            assert replay["message"]["message_id"] == str(recovery_request_id)
            reply = (await sender_client.call_tool("message_pending", {
                "api_version": "1",
                "work_id": str(sender),
                "grant_version": sender_grant.version,
            })).structured_content
            assert reply["messages"][0]["delivery_id"] == result_delivery
            assert reply["messages"][0]["message_id"] == str(result_id)


async def test_chatgpt_and_managed_mcp_processes_replay_one_durable_workflow_after_restart(
    tmp_path: Path,
):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for the MCP process test")
    assert make_url(url).database == "switchstand_test"
    subject = str(uuid4().int)
    selected, managed_grant, denied = await _provision_composed(url, subject)
    sender = selected.authority.active_work_id
    recipient = managed_grant.authority.active_work_id
    request_id, result_id, update_id = uuid4(), uuid4(), uuid4()
    port = free_port()
    run = tmp_path / str(uuid4())
    run.mkdir(mode=0o700)
    git_dir = run / "managed-git"
    git_dir.mkdir(mode=0o700)
    run_id = uuid4()
    receipt = RunReceipt(
        run_id=run_id, active_work_id=recipient, worktree=str(Path.cwd().resolve()),
        branch="fixture", pid=os.getpid(), start_token=process_start_token(os.getpid()),
        started_at=datetime.now(UTC),
    )
    (git_dir / "switchstand-run.json").write_text(receipt.model_dump_json() + "\n")
    provider_calls = run / "provider-calls.json"
    env = clean_environment() | {
        "DATABASE_URL": url, "ASANA_TOKEN": "test-only",
        "EFFECT_FILE": str(run / "effects"),
        "PROVIDER_STATE_FILE": str(run / "provider-state.json"),
        "PROVIDER_CALL_FILE": str(provider_calls),
        "SWITCHSTAND_MCP_GITHUB_CLIENT_ID": "fixture",
        "SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET": "fixture",
        "SWITCHSTAND_MCP_GITHUB_USER_ID": subject,
        "SWITCHSTAND_MCP_RESOURCE_URL": RESOURCE,
        "SWITCHSTAND_MCP_BIND_HOST": "127.0.0.1",
        "SWITCHSTAND_MCP_BIND_PORT": str(port),
    }
    managed_env = env | {
        "SWITCHSTAND_MANAGED": "1", "ACTIVE_WORK_ID": str(recipient),
        "REFERENCE_WORK_IDS": "", "SWITCHSTAND_RUN_ID": str(run_id),
        "SWITCHSTAND_WORKTREE": str(Path.cwd().resolve()),
        "SWITCHSTAND_BRANCH": "fixture", "SWITCHSTAND_GIT_DIR": str(git_dir),
    }
    server = StdioServerParameters(
        command=sys.executable, args=[__file__, "--serve-managed"], env=managed_env,
    )

    async def edge_call(endpoint, tool, arguments):
        async with Client(StreamableHttpTransport(
            endpoint + "/mcp", auth="fixed-bearer"
        )) as client:
            return (await client.call_tool(tool, arguments)).structured_content

    send = {
        "api_version": "1", "work_id": str(sender), "grant_version": selected.version,
        "message_id": str(request_id), "route_ref": "implementation",
        "recipient_work_id": str(recipient), "payload": {"request": "complete"},
    }
    with _server(env, port) as endpoint:
        async with Client(StreamableHttpTransport(
            endpoint + "/mcp", auth="fixed-bearer"
        )) as edge:
            schemas = {tool.name: tool.input_schema for tool in await edge.list_tools()}
            assert schemas["message_send"].get("additionalProperties") is False
            sent = (await edge.call_tool("message_send", send)).structured_content
            denied_send = (await edge.call_tool("message_send", send | {
                "message_id": str(uuid4()), "recipient_work_id": str(denied),
            })).structured_content
            assert denied_send == {"status": "denied", "message": None,
                                   "reason": "recipient_route_unavailable"}
        first = await _managed_workflow(server, sent, result_id, update_id)
        reply = await edge_call(endpoint, "message_pending", {
            "api_version": "1", "work_id": str(sender), "grant_version": selected.version,
        })
        updated = await edge_call(endpoint, "work_get", {
            "api_version": "1", "work_id": str(recipient),
        })
        assert reply["messages"][0]["payload"] == {"result": "complete"}
        assert updated["item"]["completed"] is True

    with _server(env, port) as endpoint:
        replayed_send = await edge_call(endpoint, "message_send", send)
        assert replayed_send["status"] == "ok"
        assert replayed_send["message"]["state"] == "DISPOSITIONED"
        for field in ("delivery_id", "message_id", "sender_work_id",
                      "recipient_work_id", "route_ref", "kind", "payload"):
            assert replayed_send["message"][field] == sent["message"][field]
        assert await _managed_replay(server, sent, result_id, update_id) == first
        replayed_reply = await edge_call(endpoint, "message_pending", {
            "api_version": "1", "work_id": str(sender), "grant_version": selected.version,
        })
        replayed_work = await edge_call(endpoint, "work_get", {
            "api_version": "1", "work_id": str(recipient),
        })
        assert replayed_reply == reply and replayed_work == updated
    assert json.loads(provider_calls.read_text())["update"] == 1


async def _managed_workflow(server, sent, result_id, update_id):
    delivery = sent["message"]["delivery_id"]
    async with MCPClient(server) as managed:
        tools = {tool.name: tool.input_schema for tool in (await managed.list_tools()).tools}
        assert tools["work_update"].get("additionalProperties") is False
        pending = (await managed.call_tool("message_pending", {
            "api_version": "1",
        })).structured_content
        assert pending["messages"][0]["delivery_id"] == delivery
        received = (await managed.call_tool("message_receive", {
            "api_version": "1", "delivery_id": delivery,
        })).structured_content
        update = (await managed.call_tool("work_update", {
            "api_version": "1", "operation_id": str(update_id),
            "observed_revision": "r1", "patch": {"completed": True},
        })).structured_content
        result = (await managed.call_tool("message_result_send", {
            "api_version": "1", "in_reply_to_delivery_id": delivery,
            "message_id": str(result_id), "payload": {"result": "complete"},
        })).structured_content
        disposed = (await managed.call_tool("message_disposition", {
            "api_version": "1", "delivery_id": delivery,
            "result_message_id": str(result_id),
        })).structured_content
        assert received["state"] in {"RECEIVED", "DISPOSITIONED"}
        assert update["effect"] == "applied" and result["status"] == "ok"
        assert disposed["state"] == "DISPOSITIONED"
        return update, result, disposed


async def _managed_replay(server, sent, result_id, update_id):
    delivery = sent["message"]["delivery_id"]
    async with MCPClient(server) as managed:
        pending = (await managed.call_tool("message_pending", {
            "api_version": "1",
        })).structured_content
        assert pending["messages"] == []
        update = (await managed.call_tool("work_update", {
            "api_version": "1", "operation_id": str(update_id),
            "observed_revision": "r1", "patch": {"completed": True},
        })).structured_content
        result = (await managed.call_tool("message_result_send", {
            "api_version": "1", "in_reply_to_delivery_id": delivery,
            "message_id": str(result_id), "payload": {"result": "complete"},
        })).structured_content
        disposed = (await managed.call_tool("message_disposition", {
            "api_version": "1", "delivery_id": delivery,
            "result_message_id": str(result_id),
        })).structured_content
        return update, result, disposed


async def _attachments(endpoint, selected, denied_work, provider_calls):
    async with Client(StreamableHttpTransport(endpoint + "/mcp", auth="fixed-bearer")) as client:
        async def call(work_id, revision):
            result = await client.call_tool("work_attachments", {
                "api_version": "1", "work_id": str(work_id), "observed_revision": revision,
            })
            assert_public(result.structured_content)
            return result.structured_content

        active = selected.authority.active_work_id
        stable = await call(active, "r1")
        assert stable == {
            "status": "ok", "work_id": str(active), "revision": "r1",
            "attachments": [{"name": "brief.txt"}], "next_cursor": None,
        }
        stale = await call(active, "old")
        assert stale == {
            "status": "stale", "work_id": str(active), "revision": "r1",
            "attachments": [], "next_cursor": None,
        }
        before_denied = json.loads(provider_calls.read_text())
        denied = await call(denied_work, "r1")
        assert denied == {
            "status": "denied", "work_id": None, "revision": None,
            "attachments": [], "next_cursor": None,
        }
        assert json.loads(provider_calls.read_text()) == before_denied


async def _boundaries(endpoint, selected, denied_work, effects):
    async with Client(StreamableHttpTransport(endpoint + "/mcp", auth="fixed-bearer")) as client:
        async def call(tool, **args):
            return (await client.call_tool(tool, {"api_version": "1", **args})).structured_content

        active = str(selected.authority.active_work_id)
        args = {"work_id": active, "grant_version": selected.version,
                "observed_revision": "r2", "text": "second"}
        for target in [str(selected.authority.reference_work_ids[0]), str(denied_work), str(uuid4())]:
            denied = await call("work_append", **(args | {"work_id": target}),
                                operation_id=str(uuid4()))
            assert denied["status"] == "denied" and denied["effect"] == "not_sent"
        assert len(json.loads(effects.read_text())) == 1
        second = await call("work_append", **args, operation_id=str(uuid4()))
        assert second["status"] == "ok"
        receipt = second["receipt"]
        readback = await call("source_story", task_gid="123", story_gid=receipt["story_gid"],
                              observed_revision="r3")
        assert readback["item"]["text"] == receipt["text"]
        first = await call("source_stories", task_gid="123", observed_revision="r3", limit=1)
        assert len(first["stories"]) == 1 and first["next_offset"] is not None
        last = await call("source_stories", task_gid="123", observed_revision="r3", limit=1,
                          offset=first["next_offset"])
        assert len(last["stories"]) == 1 and last["next_offset"] is None
        assert first["stories"][0]["story_gid"] != last["stories"][0]["story_gid"]
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

        # Independent verifier readback of the isolated provider backing state:
        # the ambiguous response produced exactly one provider story, and neither
        # same-OperationId replay nor a fresh OperationId after restart duplicated it.
        provider_rows = [ProviderSourceStory(**row) for row in json.loads(effects.read_text())]
        lost_rows = [row for row in provider_rows if row.text == "injected lost response"]
        assert len(lost_rows) == 1
        assert lost_rows[0].task_gid == "123"
        assert lost_rows[0].story_gid
        assert len(provider_rows) == 3


if __name__ == "__main__" and sys.argv[1:] == ["--serve"]:
    _child_server()
elif __name__ == "__main__" and sys.argv[1:] == ["--serve-managed"]:
    _managed_server()
