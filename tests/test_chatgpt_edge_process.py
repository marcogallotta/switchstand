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
from chatgpt_fixture import Provider, grant
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
    "grant_get", "work_get", "source_task", "source_stories", "source_story", "work_append",
    "work_create",
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
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        append_qualification="real:disposable-switchstand-test",
    )
    await grants.issue(selected, None)
    await engine.dispose()
    return selected, denied.id


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


async def _exercise(endpoint, selected, operation_id):
    transport = StreamableHttpTransport(endpoint + "/mcp", auth="fixed-bearer")
    async with Client(transport) as client:
        assert {tool.name for tool in await client.list_tools()} == TOOLS
        observed = (await client.call_tool("work_get", {"api_version": "1"})).structured_content
        assert observed["item"]["id"] == str(selected.authority.active_work_id)
        assert (await client.call_tool("grant_get", {"api_version": "1"})).structured_content[
            "grant"
        ]["id"] == str(selected.id)
        result = await client.call_tool("work_append", {
            "api_version": "1", "operation_id": str(operation_id),
            "work_id": str(selected.authority.active_work_id), "grant_version": 1,
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
    env = clean_environment() | {
        "DATABASE_URL": url, "ASANA_TOKEN": "test-only", "EFFECT_FILE": str(effects),
        "SWITCHSTAND_MCP_GITHUB_CLIENT_ID": "fixture", "SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET": "fixture",
        "SWITCHSTAND_MCP_GITHUB_USER_ID": subject, "SWITCHSTAND_MCP_RESOURCE_URL": RESOURCE,
        "SWITCHSTAND_MCP_BIND_HOST": "127.0.0.1", "SWITCHSTAND_MCP_BIND_PORT": str(port),
    }
    with _server(env, port) as endpoint:
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


async def _boundaries(endpoint, selected, denied_work, effects):
    async with Client(StreamableHttpTransport(endpoint + "/mcp", auth="fixed-bearer")) as client:
        async def call(tool, **args):
            return (await client.call_tool(tool, {"api_version": "1", **args})).structured_content

        active = str(selected.authority.active_work_id)
        args = {"work_id": active, "grant_version": 1, "observed_revision": "r2", "text": "second"}
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
