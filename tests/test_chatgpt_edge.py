import time
from uuid import uuid4

import httpx2
import pytest
from chatgpt_fixture import ACTIVE, assert_public, grant, service
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from switchstand.chatgpt_edge import (
    REQUIRED_SCOPE,
    MCPAuthConfig,
    SwitchstandGitHubProvider,
    create_app,
)
from switchstand.chatgpt_mcp import build_ordinary_tools
from switchstand.grants import PrincipalContext

RESOURCE = "https://switchstand.example.com/mcp"
ISSUER = "https://switchstand.example.com/"
GITHUB_ID = "192548"
CONFIG = MCPAuthConfig("client", "secret", GITHUB_ID, RESOURCE)


def test_config_requires_numeric_identity_exact_resource_and_loopback(monkeypatch):
    values = {
        "SWITCHSTAND_MCP_GITHUB_CLIENT_ID": "client",
        "SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET": "secret",
        "SWITCHSTAND_MCP_GITHUB_USER_ID": GITHUB_ID,
        "SWITCHSTAND_MCP_RESOURCE_URL": RESOURCE,
        "SWITCHSTAND_MCP_BIND_HOST": "127.0.0.1",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    actual = MCPAuthConfig.from_environment()
    assert (actual.base_url, actual.issuer_url) == (ISSUER, ISSUER)
    monkeypatch.setenv(
        "SWITCHSTAND_MCP_RESOURCE_URL",
        "https://SWITCHSTAND.example.com:443/internal/../mcp",
    )
    normalized = MCPAuthConfig.from_environment()
    assert normalized.resource_url == RESOURCE
    assert normalized.base_url == normalized.issuer_url == ISSUER
    with TestClient(create_app(service(), normalized, client_storage=MemoryStore())) as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["resource"] == normalized.resource_url
    monkeypatch.setenv("SWITCHSTAND_MCP_RESOURCE_URL", RESOURCE)
    for name, value, message in (
        ("SWITCHSTAND_MCP_GITHUB_USER_ID", "marco", "numeric GitHub user ID"),
        ("SWITCHSTAND_MCP_RESOURCE_URL", f"{RESOURCE}/extra", "end exactly in /mcp"),
        ("SWITCHSTAND_MCP_BIND_HOST", "0.0.0.0", "loopback-only"),
    ):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match=message):
            MCPAuthConfig.from_environment()
        monkeypatch.setenv(name, values[name])


async def test_provider_bridges_proxy_claims_and_rejects_wrong_identity_or_scope(monkeypatch):
    github_subject = GITHUB_ID

    async def verified_github(_self, token):
        return AccessToken(
            token=token, client_id="upstream", scopes=[REQUIRED_SCOPE], subject=github_subject
        )

    claims = {
        "iss": ISSUER,
        "aud": RESOURCE,
        "exp": int(time.time()) + 60,
        "client_id": "chatgpt-client",
        "scope": REQUIRED_SCOPE,
        "sub": "untrusted-proxy-subject",
    }
    subject = SwitchstandGitHubProvider(
        client_id="client",
        client_secret="secret",
        allowed_user_id=GITHUB_ID,
        base_url=ISSUER,
        issuer_url=ISSUER,
        required_scopes=[REQUIRED_SCOPE],
        client_storage=MemoryStore(),
    )
    subject.get_routes(mcp_path="/mcp")
    monkeypatch.setattr(GitHubProvider, "verify_token", verified_github)
    monkeypatch.setattr(subject.jwt_issuer, "verify_token", lambda _token: claims)
    actual = await subject.verify_token("signed-proxy-token")
    assert actual is not None
    assert actual.subject == GITHUB_ID
    assert (actual.client_id, actual.resource, actual.expires_at) == (
        "chatgpt-client", RESOURCE, claims["exp"],
    )
    assert actual.scopes == [REQUIRED_SCOPE] and actual.claims["iss"] == ISSUER
    github_subject = "999999"
    assert await subject.verify_token("signed-proxy-token") is None
    github_subject, claims["scope"] = GITHUB_ID, ""
    assert await subject.verify_token("signed-proxy-token") is None


def test_http_boundary_challenges_and_publishes_resource_and_pkce():
    app = create_app(service(), CONFIG, client_storage=MemoryStore())
    with TestClient(app) as client:
        challenge = client.post("/mcp")
        assert challenge.status_code == 401 and challenge.content == b""
        assert "resource_metadata=" in challenge.headers["www-authenticate"]
        protected = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert protected["resource"] == RESOURCE
        assert protected["scopes_supported"] == [REQUIRED_SCOPE]
        authorization = client.get("/.well-known/oauth-authorization-server").json()
        assert authorization["issuer"] == ISSUER
        assert authorization["code_challenge_methods_supported"] == ["S256"]


async def test_authenticated_registry_preserves_append_and_routes_create(monkeypatch, caplog):
    from unittest.mock import AsyncMock

    from switchstand.core import ProviderError, UnknownEffect

    caplog.set_level("INFO", logger="switchstand.chatgpt_edge")
    async def verified(_self, token):
        return AccessToken(
            token=token,
            client_id="chatgpt-client",
            scopes=[REQUIRED_SCOPE],
            subject=GITHUB_ID,
            claims={"iss": ISSUER},
            resource=RESOURCE,
            expires_at=int(time.time()) + 60,
        )

    monkeypatch.setattr(SwitchstandGitHubProvider, "verify_token", verified)
    subject = service()
    subject.grants.grant = grant(
        principal=PrincipalContext(
            issuer=ISSUER,
            subject=GITHUB_ID,
            client_id="chatgpt-client",
            assurance="authenticated",
        ),
        scope="workspace",
        operations=frozenset({"work_get", "work_search", "work_append", "work_create", "work_update"}),
        append_qualification="real:chatgpt-edge",
        create_qualification="test:chatgpt-edge",
        update_qualification="real:chatgpt-edge",
    )
    app = create_app(subject, CONFIG, client_storage=MemoryStore())

    def client_factory(**kwargs):
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=ISSUER, **kwargs
        )

    transport = StreamableHttpTransport(
        f"{ISSUER}mcp", auth="fixed-bearer", httpx_client_factory=client_factory,
    )
    async with app.router.lifespan_context(app), Client(transport) as client:
        names = {tool.name for tool in await client.list_tools()}
        grant_result = await client.call_tool("grant_get", {"api_version": "1"})
        search = await client.call_tool("work_search", {
            "api_version": "1", "text": "Task", "limit": 10,
        })
        for tool in ("work_get", "work_history", "work_event"):
            args = {"api_version": "1", "work_id": str(ACTIVE)}
            if tool != "work_get":
                args["observed_revision"] = "r1"
            if tool == "work_event":
                args["event_id"] = str(uuid4())
            result = await client.call_tool_mcp(tool, args)
            assert_public(result.model_dump(mode="json"))
        assert_public([r.message for r in caplog.records if "chatgpt_mcp tool=work_" in r.message])
        assert all(f"tool={name}" in caplog.text for name in ("work_get", "work_history", "work_event"))
        binding = await subject.state.bind_event(ACTIVE, "asana", "123", "raw-event")
        for failure, expected in ((UnknownEffect("private-provider-detail"), "unknown"),
                                  (ProviderError("private-provider-detail"), "provider_error")):
            with monkeypatch.context() as patch:
                for method in ("get", "source_task", "source_stories"):
                    patch.setattr(subject.providers["asana"], method, AsyncMock(side_effect=failure))
                for tool in ("work_get", "work_history", "work_event"):
                    args = {"api_version": "1", "work_id": str(ACTIVE)}
                    if tool != "work_get":
                        args["observed_revision"] = "r1"
                    if tool == "work_event":
                        args["event_id"] = str(binding.id)
                    result = await client.call_tool_mcp(tool, args)
                    assert result.structured_content["status"] == expected
                    assert_public(result.model_dump(mode="json"))
                    assert "private-provider-detail" not in str(result)
        assert "private-provider-detail" not in caplog.text
        operation_id = str(uuid4())
        append = await client.call_tool("work_append", {
            "api_version": "1",
            "operation_id": operation_id,
            "work_id": str(ACTIVE),
            "grant_version": 1,
            "observed_revision": "r1",
            "text": "ChatGPT authorized feedback",
        })
        replay = await client.call_tool("work_append", {
            "api_version": "1",
            "operation_id": operation_id,
            "work_id": str(ACTIVE),
            "grant_version": 1,
            "observed_revision": "r1",
            "text": "ChatGPT authorized feedback",
        })
        create = await client.call_tool("work_create", {
            "api_version": "1",
            "operation_id": str(uuid4()),
            "parent_work_id": str(ACTIVE),
            "grant_version": 1,
            "title": "Qualified child",
            "notes": "edge route proof",
        })
        updated = await client.call_tool("work_update", {
            "api_version": "1", "operation_id": str(uuid4()), "work_id": str(ACTIVE),
            "grant_version": 1, "observed_revision": "r2", "patch": {"completed": True},
        })
    assert names == {name for name, _ in build_ordinary_tools(subject)}
    assert grant_result.structured_content["principal"]["subject"] == GITHUB_ID
    assert search.structured_content["status"] == "ok"
    assert search.structured_content["items"][0]["id"] == str(ACTIVE)
    assert "provider" not in search.structured_content["items"][0]
    assert "task_gid" not in search.structured_content["items"][0]
    assert append.structured_content["status"] == "ok"
    assert replay.structured_content == append.structured_content
    assert create.structured_content["status"] == "denied"
    assert create.structured_content["reason"] == "provider_create_not_supported"
    assert updated.structured_content["status"] == "ok"
    assert subject.providers["asana"].sends == 1
