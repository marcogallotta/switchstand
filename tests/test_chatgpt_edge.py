import time
from uuid import uuid4

import httpx2
import pytest
from chatgpt_fixture import ACTIVE, PRINCIPAL, assert_public, grant, service
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from switchstand import chatgpt_edge
from switchstand.activation_continuity import ActivationContract
from switchstand.chatgpt_edge import (
    REQUIRED_SCOPE,
    MCPAuthConfig,
    SwitchstandGitHubProvider,
    create_app,
)
from switchstand.chatgpt_mcp import build_ordinary_tools
from switchstand.grants import PrincipalContext
from switchstand.priority_claim_service import PriorityClaimService
from switchstand.priority_context import PriorityContextProjection
from switchstand.product_currentness_stateful import StatefulPersistenceSnapshot

RESOURCE = "https://switchstand.example.com/mcp"
ISSUER = "https://switchstand.example.com/"
GITHUB_ID = "192548"
CONFIG = MCPAuthConfig("client", "secret", GITHUB_ID, RESOURCE)


def _currentness_environment(monkeypatch):
    values = {
        "SWITCHSTAND_PRODUCT_CURRENTNESS": "1",
        "SWITCHSTAND_PRODUCT_CURRENTNESS_RUNTIME_SHA": "a" * 40,
        "SWITCHSTAND_PRODUCT_CURRENTNESS_SELECTED_RUNTIME_SHA": "a" * 40,
        "SWITCHSTAND_PRODUCT_CURRENTNESS_RUN_ID": "run-1",
        "SWITCHSTAND_PRODUCT_CURRENTNESS_EXPECTED_TOOLS_SCHEMA_SHA256": "b" * 64,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


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
    assert app.state.fastmcp_server.auth._fastmcp_access_token_expiry_seconds == 31_536_000
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


def test_certification_runtime_readback_is_explicit_and_exact():
    ordinary = create_app(service(), CONFIG, client_storage=MemoryStore())
    certification = create_app(
        service(), CONFIG, client_storage=MemoryStore(),
        certification_runtime=("a" * 40, "run-1"),
    )
    with TestClient(ordinary) as client:
        assert client.get("/.well-known/switchstand-certification-runtime").status_code == 404
    with TestClient(certification) as client:
        assert client.get("/.well-known/switchstand-certification-runtime").json() == {
            "runtime_sha": "a" * 40,
            "run_id": "run-1",
        }


def test_product_currentness_configuration_is_default_off_and_fails_closed(monkeypatch, tmp_path):
    assert chatgpt_edge._ProductCurrentnessConfig.from_environment() is None
    monkeypatch.setenv("SWITCHSTAND_PRODUCT_CURRENTNESS", "yes")
    with pytest.raises(ValueError, match="must be 0 or 1"):
        chatgpt_edge._ProductCurrentnessConfig.from_environment()

    values = _currentness_environment(monkeypatch)
    monkeypatch.delenv("SWITCHSTAND_PRODUCT_CURRENTNESS_RUN_ID")
    with pytest.raises(ValueError, match="RUN_ID"):
        chatgpt_edge._ProductCurrentnessConfig.from_environment()
    monkeypatch.setenv("SWITCHSTAND_PRODUCT_CURRENTNESS_RUN_ID", values[
        "SWITCHSTAND_PRODUCT_CURRENTNESS_RUN_ID"
    ])
    monkeypatch.setenv("SWITCHSTAND_PRODUCT_CURRENTNESS_RUNTIME_SHA", "not-a-sha")
    with pytest.raises(ValueError, match="runtime SHA"):
        chatgpt_edge._ProductCurrentnessConfig.from_environment()

    monkeypatch.setenv(
        "SWITCHSTAND_PRODUCT_CURRENTNESS_RUNTIME_SHA",
        values["SWITCHSTAND_PRODUCT_CURRENTNESS_RUNTIME_SHA"],
    )
    monkeypatch.setenv(
        "SWITCHSTAND_PRODUCT_CURRENTNESS_QUALIFICATION_RECEIPT",
        str(tmp_path / "receipt.json"),
    )
    with pytest.raises(ValueError, match="must be configured together"):
        chatgpt_edge._ProductCurrentnessConfig.from_environment()
    monkeypatch.setenv("SWITCHSTAND_PRODUCT_CURRENTNESS_QUALIFICATION_KEY", "relative.key")
    with pytest.raises(ValueError, match="paths must be absolute"):
        chatgpt_edge._ProductCurrentnessConfig.from_environment()

    key_path = tmp_path / "qualification.key"
    monkeypatch.setenv("SWITCHSTAND_PRODUCT_CURRENTNESS_QUALIFICATION_KEY", str(key_path))
    configured = chatgpt_edge._ProductCurrentnessConfig.from_environment()
    assert (configured.qualification_receipt, configured.qualification_key) == (
        tmp_path / "receipt.json",
        key_path,
    )


async def test_resource_edge_currentness_diagnostic_cannot_claim_true(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://ignored")

    class Engine:
        async def dispose(self):
            pass

    monkeypatch.setattr(chatgpt_edge, "create_async_engine", lambda _url: Engine())
    monkeypatch.setattr(chatgpt_edge, "register_sqlalchemy_timing", lambda _engine: None)

    async def persistence(_self):
        return StatefulPersistenceSnapshot(
            migration_revision=chatgpt_edge.STATEFUL_MIGRATION_REVISION,
            migration_receipt_digest="c" * 64,
            outcome_state_table="outcome_state_revisions",
        )

    monkeypatch.setattr(chatgpt_edge.LiveStatefulEvidenceReader, "_persistence", persistence)
    async with chatgpt_edge.resource_service() as (subject, _runtime):
        subject.outcome_state_enabled = True
        subject.product_currentness_enabled = True

        async def placeholder(_principal):
            raise AssertionError("inventory construction must not call the tool")

        subject.product_currentness = placeholder
        preliminary = create_app(subject, CONFIG, client_storage=MemoryStore())
        preliminary_tools = await preliminary.state.fastmcp_server.list_tools(
            run_middleware=False
        )
        _, expected_digest = chatgpt_edge._tools_snapshot(preliminary_tools)

        _currentness_environment(monkeypatch)
        monkeypatch.setenv(
            "SWITCHSTAND_PRODUCT_CURRENTNESS_EXPECTED_TOOLS_SCHEMA_SHA256",
            expected_digest,
        )
        subject.product_currentness = None
        subject.product_currentness_enabled = False
        captured = []
        reader_configuration = []
        reader_type = chatgpt_edge.LiveStatefulEvidenceReader

        def configured_reader(*args, **kwargs):
            reader_configuration.append(kwargs)
            return reader_type(*args, **kwargs)

        monkeypatch.setattr(chatgpt_edge, "LiveStatefulEvidenceReader", configured_reader)
        build_tools = chatgpt_edge.build_ordinary_tools

        def capture_tools(wired, *args, **kwargs):
            captured.append(wired)
            return build_tools(wired, *args, **kwargs)

        monkeypatch.setattr(chatgpt_edge, "build_ordinary_tools", capture_tools)
        create_app(subject, CONFIG, client_storage=MemoryStore())
        wired = captured[-1]
        assert wired.product_currentness_enabled is True
        assert wired.product_currentness is not None
        result = await wired.product_currentness(PRINCIPAL)

        assert reader_configuration[-1]["qualification_receipt"] is None
        assert reader_configuration[-1]["qualification_key"] is None
        assert (result.status, result.current) == ("unknown", "UNKNOWN")
        assert result.blockers == ("functional_proof",)

        receipt_path = tmp_path / "receipt.json"
        key_path = tmp_path / "qualification.key"
        monkeypatch.setenv(
            "SWITCHSTAND_PRODUCT_CURRENTNESS_QUALIFICATION_RECEIPT", str(receipt_path)
        )
        monkeypatch.setenv(
            "SWITCHSTAND_PRODUCT_CURRENTNESS_QUALIFICATION_KEY", str(key_path)
        )
        create_app(subject, CONFIG, client_storage=MemoryStore())
        wired_with_paths = captured[-1]
        configured_result = await wired_with_paths.product_currentness(PRINCIPAL)

    assert reader_configuration[-1]["qualification_receipt"] == receipt_path
    assert reader_configuration[-1]["qualification_key"] == key_path
    assert (configured_result.status, configured_result.current) == ("unknown", "UNKNOWN")
    assert configured_result.blockers == ("functional_proof",)
    assert configured_result.conditions[-1].detail == "functional_proof_missing_or_invalid"


async def test_resource_edge_preserves_injected_services(monkeypatch):
    captured = []
    build_tools = chatgpt_edge.build_ordinary_tools

    def capture_tools(subject, *args, **kwargs):
        captured.append(subject)
        return build_tools(subject, *args, **kwargs)

    monkeypatch.setattr(chatgpt_edge, "build_ordinary_tools", capture_tools)
    plain_app = create_app(service(), CONFIG, client_storage=MemoryStore())
    plain_tools = {tool.name for tool in await plain_app.state.fastmcp_server.list_tools()}
    assert "activation_obligation_transition" not in plain_tools

    subject = service()
    dependencies = [object() for _ in range(7)]
    (
        subject.priority_claims,
        subject.priority_context,
        subject.reviews,
        subject.activation_continuity,
        subject.activation_technical,
        subject.activation_runtime,
        subject.activation_proof,
    ) = dependencies
    subject.priority_claims_enabled = True
    subject.priority_context_enabled = True
    injected_app = create_app(subject, CONFIG, client_storage=MemoryStore())
    injected_tools = {tool.name for tool in await injected_app.state.fastmcp_server.list_tools()}

    assert {
        "priority_claim_get", "priority_claim_record", "priority_context_get",
        "review_request", "review_submit", "activation_obligation_transition",
    } <= injected_tools
    assert [
        captured[-1].priority_claims,
        captured[-1].priority_context,
        captured[-1].reviews,
        captured[-1].activation_continuity,
        captured[-1].activation_technical,
        captured[-1].activation_runtime,
        captured[-1].activation_proof,
    ] == dependencies


async def test_resource_service_priority_surface_is_explicit_and_default_off(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://ignored")

    class Engine:
        async def dispose(self):
            pass

    engine = Engine()
    monkeypatch.setattr(chatgpt_edge, "create_async_engine", lambda _url: engine)
    monkeypatch.setattr(chatgpt_edge, "register_sqlalchemy_timing", lambda _engine: None)

    async with chatgpt_edge.resource_service() as (plain, _runtime):
        assert plain.priority_claims is None
        assert plain.priority_context is None
        assert plain.priority_claims_enabled is False
        assert plain.priority_context_enabled is False

    monkeypatch.setenv("SWITCHSTAND_PRIORITY_CLAIMS", "1")
    async with chatgpt_edge.resource_service() as (enabled, _runtime):
        assert isinstance(enabled.priority_claims, PriorityClaimService)
        assert isinstance(enabled.priority_context, PriorityContextProjection)
        assert enabled.priority_claims_enabled is True
        assert enabled.priority_context_enabled is True


async def test_resource_service_activation_registry_is_explicit_and_default_off(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://ignored")

    class Engine:
        async def dispose(self):
            pass

    monkeypatch.setattr(chatgpt_edge, "create_async_engine", lambda _url: Engine())
    monkeypatch.setattr(chatgpt_edge, "register_sqlalchemy_timing", lambda _engine: None)
    product = uuid4()
    contract = ActivationContract(
        product_work_id=product,
        outcome_key="release",
        target_revision="git:abc",
        target_phase="ACTIVATED",
        return_owner_work_id=uuid4(),
        acceptance_contract_id="acceptance-v1",
        contract_revision="v1",
        acceptance_verifier_work_id=uuid4(),
        adoption_requirement="NOT_REQUIRED",
        adoption_actor_work_id=None,
        lifecycle_authority_work_id=product,
    )
    contracts = {contract.obligation_id: contract}

    async with chatgpt_edge.resource_service() as (plain, _runtime):
        assert plain.activation_continuity is None
    async with chatgpt_edge.resource_service(contracts) as (injected, _runtime):
        assert injected.activation_continuity is not None
        assert injected.activation_continuity.contracts is contracts


async def test_stateful_http_session_is_stable_distinct_and_credential_bound(monkeypatch):
    async def verified(_self, token):
        subject, client_id = (
            (GITHUB_ID, "client-a") if token == "token-a" else ("999999", "client-b")
        )
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=[REQUIRED_SCOPE],
            subject=subject,
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
            client_id="client-a",
            assurance="authenticated",
        ),
        scope="workspace",
        operations=frozenset({"message"}),
    )
    app = create_app(subject, CONFIG, client_storage=MemoryStore())
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {"name": "session-proof", "version": "1"},
            "protocolVersion": "2025-03-26",
            "capabilities": {},
        },
    }
    base_headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }

    async with app.router.lifespan_context(app), httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=ISSUER
    ) as raw:
        first = await raw.post(
            "/mcp",
            headers=base_headers | {"authorization": "Bearer token-a"},
            json=initialize,
        )
        assert first.status_code == 200
        first_session = first.headers["mcp-session-id"]
        session_headers = base_headers | {
            "authorization": "Bearer token-a",
            "mcp-session-id": first_session,
            "mcp-protocol-version": "2025-03-26",
        }
        ready = await raw.post(
            "/mcp",
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert ready.status_code == 202
        for request_id in (2, 3):
            listed = await raw.post(
                "/mcp",
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/list",
                    "params": {},
                },
            )
            assert listed.status_code == 200

        second = await raw.post(
            "/mcp",
            headers=base_headers | {"authorization": "Bearer token-a"},
            json=initialize | {"id": 4},
        )
        assert second.status_code == 200
        second_session = second.headers["mcp-session-id"]
        assert second_session != first_session

        rejected = await raw.post(
            "/mcp",
            headers=session_headers | {"authorization": "Bearer token-b"},
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/list",
                "params": {},
            },
        )
        assert rejected.status_code == 404
        assert "Session not found" in rejected.text


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
        create_qualification="real:chatgpt-edge",
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
        tools = await client.list_tools()
        names = {tool.name for tool in tools}
        for tool in tools:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is False
        search = await client.call_tool("work_search", {
            "api_version": "1", "text": "Task", "limit": 10,
        })
        for tool in ("work_get", "work_history", "work_event"):
            args = {"api_version": "1", "work_id": str(ACTIVE)}
            if tool != "work_get":
                args["observed_revision"] = "r1"
                args["purpose"] = "investigation"
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
                        args["purpose"] = "investigation"
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
            "observed_revision": "r1",
            "text": "ChatGPT authorized feedback",
            "purpose": "provenance",
        })
        replay = await client.call_tool("work_append", {
            "api_version": "1",
            "operation_id": operation_id,
            "work_id": str(ACTIVE),
            "observed_revision": "r1",
            "text": "ChatGPT authorized feedback",
            "purpose": "provenance",
        })
        create = await client.call_tool("work_create", {
            "api_version": "1",
            "operation_id": str(uuid4()),
            "parent_work_id": str(ACTIVE),
            "title": "Qualified child",
            "notes": "edge route proof",
        })
        updated = await client.call_tool("work_update", {
            "api_version": "1", "operation_id": str(uuid4()), "work_id": str(ACTIVE),
            "observed_revision": "r2", "patch": {"completed": True},
        })
    assert names == {name for name, _ in build_ordinary_tools(subject)}
    assert "grant_get" not in names
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
