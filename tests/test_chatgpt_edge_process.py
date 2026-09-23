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
from mcp.server.auth.provider import AccessToken
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.core import ProviderSourceStory, UnknownEffect
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext
from switchstand.state import PostgresState

TOOLS = {
    "grant_get", "work_get", "work_search", "source_task", "source_stories",
    "source_story", "work_history", "work_attachments", "work_event", "work_append", "work_create",
}
ISSUER = "https://switchstand.example/"
RESOURCE = ISSUER + "mcp"
CLIENT_ID = "chatgpt-client"


class CountingProvider(Provider):
    """Persist only the synthetic provider's stories across edge restarts."""

    def __init__(self):
        super().__init__()
        self.path = Path(os.environ["EFFECT_FILE"])
        if self.path.exists():
            self.stories = [ProviderSourceStory(**row) for row in json.loads(self.path.read_text())]
        self.sends = len(self.stories)
        self.revision = f"r{self.sends + 1}"

    def _count(self, operation):
        path = Path(os.environ["PROVIDER_CALL_FILE"])
        counts = json.loads(path.read_text()) if path.exists() else {}
        counts[operation] = counts.get(operation, 0) + 1
        path.write_text(json.dumps(counts))

    async def get(self, task_gid):
        self._count("get")
        return await super().get(task_gid)

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
    edge.main()


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
            if tool.name in {"work_search", "work_get", "work_history", "work_attachments", "work_event"}:
                assert_public(tool.model_dump(mode="json"))
                assert tool.inputSchema.get("additionalProperties") is False
                if tool.name == "work_attachments":
                    schema = tool.inputSchema
                    assert set(schema["required"]) == {"api_version", "work_id", "observed_revision"}
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
        assert len(json.loads(effects.read_text())) == 3


if __name__ == "__main__" and sys.argv[1:] == ["--serve"]:
    _child_server()
