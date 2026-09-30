import asyncio
import time

import httpx
import pytest
from chatgpt_fixture import service
from fastmcp.server.auth.auth import AccessToken
from key_value.aio.stores.memory import MemoryStore
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from switchstand.chatgpt_edge import create_delegated_app
from switchstand.stable_auth import (
    INTROSPECTION_CLOCK_SKEW_SECONDS,
    INTROSPECTION_PATH,
    IntrospectionContract,
    IntrospectionTokenVerifier,
    StableAuthConfig,
    create_auth_service,
)

ISSUER = "https://switchstand.example.com/"
RESOURCE = ISSUER + "mcp"
USER_ID = "192548"
SCOPE = "read:user"
INTERNAL_SECRET = "private-loopback-secret"
CONTRACT = IntrospectionContract.for_resource(RESOURCE, USER_ID)
CONFIG = StableAuthConfig("github-client", "github-secret", INTERNAL_SECRET, CONTRACT)


def test_contract_and_service_configuration_are_closed_and_separate():
    assert (CONTRACT.issuer_url, CONTRACT.resource_url, CONTRACT.scopes) == (
        ISSUER,
        RESOURCE,
        (SCOPE,),
    )
    for resource, user, message in (
        ("http://switchstand.example.com/mcp", USER_ID, "absolute HTTPS"),
        (RESOURCE + "/extra", USER_ID, "end exactly"),
        (RESOURCE, "marco", "numeric"),
    ):
        with pytest.raises(ValueError, match=message):
            IntrospectionContract.for_resource(resource, user)
    with pytest.raises(ValueError, match="introspection secret"):
        StableAuthConfig("client", "secret", "", CONTRACT)
    with pytest.raises(ValueError, match="exact and normalized"):
        IntrospectionContract("https://wrong.example/", RESOURCE, USER_ID)
    assert "github-secret" not in repr(CONFIG)
    assert INTERNAL_SECRET not in repr(CONFIG)


def test_auth_service_owns_public_oauth_routes_and_not_the_resource_endpoint():
    app = create_auth_service(CONFIG, client_storage=MemoryStore())
    with TestClient(app) as client:
        metadata = client.get("/.well-known/oauth-authorization-server").json()
        assert metadata["issuer"] == ISSUER
        assert metadata["registration_endpoint"] == ISSUER + "register"
        assert metadata["code_challenge_methods_supported"] == ["S256"]
        assert client.post("/mcp").status_code == 404


async def test_private_service_and_verifier_preserve_identity_and_immediate_revocation():
    app = create_auth_service(CONFIG, client_storage=MemoryStore())
    provider = app.state.auth_provider
    active = True

    async def verify(token):
        if not active or token not in {"token-a", "token-b"}:
            return None
        return AccessToken(
            token=token,
            client_id=f"client-{token[-1]}",
            scopes=[SCOPE],
            subject=USER_ID,
            expires_at=int(time.time()) + 60,
            resource=RESOURCE,
            claims={"iss": ISSUER},
        )

    provider.verify_token = verify
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://auth.internal") as client:
        verifier = IntrospectionTokenVerifier(
            client, internal_secret=INTERNAL_SECRET, contract=CONTRACT
        )
        first, second = await asyncio.gather(
            verifier.verify_token("token-a"), verifier.verify_token("token-b")
        )
        assert first is not None and first.client_id == "client-a"
        assert second is not None and second.client_id == "client-b"
        active = False
        assert await verifier.verify_token("token-a") is None


async def test_private_service_authentication_input_and_provider_errors_fail_closed():
    app = create_auth_service(CONFIG, client_storage=MemoryStore())
    provider = app.state.auth_provider

    async def explode(_token):
        raise RuntimeError("provider detail must not cross the boundary")

    provider.verify_token = explode
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://auth.internal") as client:
        assert (await client.post(INTROSPECTION_PATH, json={"token": "x"})).status_code == 401
        assert (
            await client.post(
                INTROSPECTION_PATH,
                headers={"Authorization": f"Bearer {INTERNAL_SECRET}"},
                json={"not_token": "x"},
            )
        ).status_code == 400
        failed = await client.post(
            INTROSPECTION_PATH,
            headers={"Authorization": f"Bearer {INTERNAL_SECRET}"},
            json={"token": "x"},
        )
        assert failed.status_code == 503 and failed.content == b""


async def test_authenticated_malformed_success_replies_fail_closed():
    now = 1_000_000
    valid = {
        "client_id": "chatgpt",
        "scopes": [SCOPE],
        "subject": USER_ID,
        "expires_at": now + INTROSPECTION_CLOCK_SKEW_SECONDS + 1,
        "resource": RESOURCE,
        "issuer": ISSUER,
    }

    async def verify_payload(payload):
        async def reply(request):
            assert request.headers["authorization"] == f"Bearer {INTERNAL_SECRET}"
            return JSONResponse(payload)

        app = Starlette(routes=[Route(INTROSPECTION_PATH, reply, methods=["POST"])])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://auth.internal"
        ) as client:
            return await IntrospectionTokenVerifier(
                client,
                internal_secret=INTERNAL_SECRET,
                contract=CONTRACT,
                clock=lambda: now,
            ).verify_token("token")

    assert await verify_payload(valid) is not None
    invalid = [
        [],
        {**valid, "extra": "unbounded"},
        {**valid, "client_id": ""},
        {**valid, "client_id": "x" * 5000},
        {**valid, "subject": "999999"},
        {**valid, "issuer": "https://evil.example/"},
        {**valid, "resource": ISSUER + "other"},
        {**valid, "expires_at": True},
        {**valid, "expires_at": now - 1},
        {**valid, "expires_at": now + INTROSPECTION_CLOCK_SKEW_SECONDS},
        {**valid, "scopes": SCOPE},
        {**valid, "scopes": [SCOPE, 7]},
        {**valid, "scopes": [""]},
        {**valid, "scopes": [SCOPE, "repo"]},
    ]
    for payload in invalid:
        assert await verify_payload(payload) is None


async def test_introspection_unavailability_fails_closed():
    def unavailable(request):
        raise httpx.ConnectError("auth service unavailable", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unavailable), base_url="http://auth.internal"
    ) as client:
        verifier = IntrospectionTokenVerifier(
            client, internal_secret=INTERNAL_SECRET, contract=CONTRACT
        )
        assert await verifier.verify_token("token") is None


def test_delegated_edge_has_resource_metadata_without_provider_or_signing_state():
    client = httpx.AsyncClient(base_url="http://auth.internal")
    verifier = IntrospectionTokenVerifier(
        client, internal_secret=INTERNAL_SECRET, contract=CONTRACT
    )
    app = create_delegated_app(service(), verifier)
    try:
        with TestClient(app) as edge:
            protected = edge.get("/.well-known/oauth-protected-resource/mcp").json()
            assert protected["resource"] == RESOURCE
            assert protected["authorization_servers"] == [ISSUER]
            assert protected["scopes_supported"] == [SCOPE]
            assert protected["bearer_methods_supported"] == ["header"]
            assert edge.post("/mcp").status_code == 401
        auth = app.state.fastmcp_server.auth
        assert auth.token_verifier is verifier
        assert not hasattr(auth, "client_secret")
        assert not hasattr(auth, "jwt_issuer")
    finally:
        asyncio.run(client.aclose())
