"""Real process/socket/PostgreSQL replay; token verification and provider are fixtures."""

import os
import socket
import subprocess
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
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AccessToken
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext
from switchstand.state import PostgresState, metadata

TOOLS = {
    "grant_get", "work_get", "source_task", "source_stories", "source_story", "work_append",
    "work_create",
}
ISSUER = "https://switchstand.example/"
RESOURCE = ISSUER + "mcp"
CLIENT_ID = "chatgpt-client"


class CountingProvider(Provider):
    async def get(self, task_gid):
        return await super().get("123")

    async def source_task(self, task_gid):
        return await super().source_task("123")

    async def append(self, task_gid, text):
        _record_effect()
        return await super().append(task_gid, text)


def _record_effect():
    with Path(os.environ["EFFECT_FILE"]).open("a") as effects:
        effects.write("sent\n")


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
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    state, grants = PostgresState(engine), GrantState(engine)
    active = await state.bind("asana", "vertical-" + str(uuid4()))
    principal = PrincipalContext(
        issuer=ISSUER, subject=subject, client_id=CLIENT_ID, assurance="authenticated",
    )
    selected = grant(
        principal=principal, active=active.id, reference=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        append_qualification="real:disposable-switchstand-test",
    )
    await grants.issue(selected, None)
    await engine.dispose()
    return selected


def _free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def _server(env, port):
    process = subprocess.Popen(
        [sys.executable, __file__, "--serve"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    endpoint = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        with httpx.Client(trust_env=False, timeout=0.2) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
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
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


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


async def test_process_with_fixture_identity_replays_durable_append_after_restart(tmp_path):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for the MCP process test")
    assert make_url(url).database == "switchstand_test"
    subject = str(uuid4().int)
    selected, operation_id, port = await _provision(url, subject), uuid4(), _free_port()
    effects = tmp_path / "effects"
    env = os.environ | {
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
        assert effects.read_text().splitlines() == ["sent"]


if __name__ == "__main__" and sys.argv[1:] == ["--serve"]:
    _child_server()
