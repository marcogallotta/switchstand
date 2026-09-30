import hmac
import time

import fastmcp
import httpx
from fastmcp.server.auth import RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy.models import ClientCode
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AuthorizationCode
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl, AnyUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from switchstand.oauth_continuity import SwitchstandGitHubProvider
from switchstand.stable_auth_migration import WRITER_SERVICES, copy_with_receipt

ISSUER = "https://switchstand.example.com/"
RESOURCE = "https://switchstand.example.com/mcp"
USER_ID = "192548"
SCOPE = "read:user"
INTERNAL_SECRET = "disposable-loopback-auth-secret"
INTROSPECTION_CLOCK_SKEW_SECONDS = 5
MIGRATION_SECRET = "github-secret-material-with-more-than-32-bytes"


class StoppedWriters:
    def inactive(self):
        return {service: True for service in WRITER_SERVICES}


def auth_provider(*, storage=None, client_secret="github-secret"):
    storage = storage or MemoryStore()
    subject = SwitchstandGitHubProvider(
        client_id="github-client",
        client_secret=client_secret,
        allowed_user_id=USER_ID,
        base_url=ISSUER,
        resource_base_url=ISSUER,
        issuer_url=ISSUER,
        required_scopes=[SCOPE],
        client_storage=storage,
    )
    subject.get_routes(mcp_path="/mcp")
    return subject


def default_storage_provider(*, client_secret="github-secret"):
    subject = SwitchstandGitHubProvider(
        client_id="github-client",
        client_secret=client_secret,
        allowed_user_id=USER_ID,
        base_url=ISSUER,
        resource_base_url=ISSUER,
        issuer_url=ISSUER,
        required_scopes=[SCOPE],
    )
    subject.get_routes(mcp_path="/mcp")
    return subject


def introspection_app(provider, *, internal_secret=INTERNAL_SECRET):
    async def verify(request: Request):
        expected = f"Bearer {internal_secret}"
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, expected):
            return Response(status_code=401)
        try:
            payload = await request.json()
            token = payload.get("token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                return Response(status_code=400)
            access = await provider.verify_token(token)
        except Exception:  # noqa: BLE001 -- private boundary fails closed
            return Response(status_code=503)
        if access is None:
            return Response(status_code=401)
        return JSONResponse(
            {
                "client_id": access.client_id,
                "scopes": access.scopes,
                "subject": access.subject,
                "expires_at": access.expires_at,
                "resource": access.resource,
                "issuer": (access.claims or {}).get("iss"),
            }
        )

    return Starlette(routes=[Route("/internal/oauth/verify", verify, methods=["POST"])])


class IntrospectionTokenVerifier(TokenVerifier):
    """Delegate complete token/JTI/upstream validation to the stable auth service."""

    def __init__(
        self,
        client,
        *,
        internal_secret=INTERNAL_SECRET,
        clock=time.time,
    ):
        super().__init__(required_scopes=[SCOPE])
        self.client = client
        self.internal_secret = internal_secret
        self.clock = clock

    async def verify_token(self, token):
        try:
            response = await self.client.post(
                "/internal/oauth/verify",
                headers={"Authorization": f"Bearer {self.internal_secret}"},
                json={"token": token},
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        client_id = payload.get("client_id")
        expires_at = payload.get("expires_at")
        scopes = payload.get("scopes")
        if (
            payload.get("subject") != USER_ID
            or payload.get("issuer") != ISSUER
            or payload.get("resource") != RESOURCE
            or not isinstance(client_id, str)
            or not client_id.strip()
            or type(expires_at) is not int
            or expires_at <= int(self.clock()) + INTROSPECTION_CLOCK_SKEW_SECONDS
            or not isinstance(scopes, list)
            or len(scopes) != 1
            or scopes != [SCOPE]
        ):
            return None
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at,
            subject=payload["subject"],
            resource=payload["resource"],
            claims={"iss": payload["issuer"]},
        )


def edge_auth(verifier):
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(ISSUER)],
        base_url=ISSUER,
        scopes_supported=[SCOPE],
    )


def fake_github(subject, state):
    async def verify(token):
        if token != "github-token" or not state["valid"]:
            return None
        return AccessToken(
            token=token,
            client_id="github",
            scopes=[SCOPE],
            subject=USER_ID,
        )

    subject._token_validator.verify_token = verify


async def issue(subject, state, client_id="chatgpt"):
    fake_github(subject, state)
    await subject._code_store.put(
        key="code",
        value=ClientCode(
            code="code",
            client_id=client_id,
            redirect_uri="https://client.example/callback",
            code_challenge="challenge",
            code_challenge_method="S256",
            scopes=[SCOPE],
            idp_tokens={
                "access_token": "github-token",
                "expires_in": 1800,
                "scope": SCOPE,
            },
            expires_at=time.time() + 300,
            created_at=time.time(),
        ),
        ttl=300,
    )
    return await subject.exchange_authorization_code(
        OAuthClientInformationFull(client_id=client_id),
        AuthorizationCode(
            code="code",
            client_id=client_id,
            redirect_uri=AnyUrl("https://client.example/callback"),
            redirect_uri_provided_explicitly=True,
            scopes=[SCOPE],
            expires_at=time.time() + 300,
            code_challenge="challenge",
        ),
    )


def test_split_routes_retain_one_public_issuer_and_resource_contract():
    authorization = auth_provider()
    resource = edge_auth(TokenVerifier(required_scopes=[SCOPE]))
    auth_routes = {route.path for route in authorization.get_routes("/mcp")}
    assert {"/authorize", "/token", "/register", "/auth/callback", "/consent"} <= auth_routes

    with TestClient(Starlette(routes=authorization.get_routes("/mcp"))) as client:
        metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["issuer"] == ISSUER
    assert metadata["registration_endpoint"] == ISSUER + "register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["client_id_metadata_document_supported"] is True

    with TestClient(Starlette(routes=resource.get_routes("/mcp"))) as client:
        protected = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert protected["resource"] == RESOURCE
    assert protected["authorization_servers"] == [ISSUER]
    assert protected["scopes_supported"] == [SCOPE]


async def test_edge_delegates_current_upstream_validation_and_revocation():
    state = {"valid": True}
    authorization = auth_provider()
    issued = await issue(authorization, state)
    transport = httpx.ASGITransport(app=introspection_app(authorization))
    async with httpx.AsyncClient(transport=transport, base_url="http://auth.internal") as client:
        verifier = IntrospectionTokenVerifier(client)
        access = await verifier.verify_token(issued.access_token)
        assert access is not None
        assert (access.subject, access.client_id, access.resource) == (
            USER_ID,
            "chatgpt",
            RESOURCE,
        )
        assert await verifier.verify_token("not-a-token") is None

        state["valid"] = False
        assert await verifier.verify_token(issued.access_token) is None


async def test_private_introspection_boundary_fails_closed():
    state = {"valid": True}
    authorization = auth_provider()
    issued = await issue(authorization, state)
    transport = httpx.ASGITransport(app=introspection_app(authorization))
    async with httpx.AsyncClient(transport=transport, base_url="http://auth.internal") as client:
        assert await IntrospectionTokenVerifier(
            client, internal_secret="wrong-secret"
        ).verify_token(issued.access_token) is None

    now = 1_000_000
    valid_payload = {
        "client_id": "chatgpt",
        "scopes": [SCOPE],
        "subject": USER_ID,
        "expires_at": now + INTROSPECTION_CLOCK_SKEW_SECONDS + 1,
        "resource": RESOURCE,
        "issuer": ISSUER,
    }

    async def verify_reply(payload):
        async def reply(request):
            assert request.headers["authorization"] == f"Bearer {INTERNAL_SECRET}"
            return JSONResponse(payload)

        malformed = Starlette(
            routes=[Route("/internal/oauth/verify", reply, methods=["POST"])]
        )
        malformed_transport = httpx.ASGITransport(app=malformed)
        async with httpx.AsyncClient(
            transport=malformed_transport, base_url="http://auth.internal"
        ) as client:
            return await IntrospectionTokenVerifier(
                client, clock=lambda: now
            ).verify_token(issued.access_token)

    assert await verify_reply(valid_payload) is not None
    invalid_payloads = [
        {**valid_payload, "client_id": ""},
        {**valid_payload, "client_id": "   "},
        {**valid_payload, "expires_at": now - 1},
        {**valid_payload, "expires_at": True},
        {
            **valid_payload,
            "expires_at": now + INTROSPECTION_CLOCK_SKEW_SECONDS,
        },
        {**valid_payload, "scopes": SCOPE},
        {**valid_payload, "scopes": [SCOPE, 7]},
        {**valid_payload, "scopes": [""]},
        {**valid_payload, "scopes": [SCOPE, "repo"]},
    ]
    for payload in invalid_payloads:
        assert await verify_reply(payload) is None

    def unavailable(request):
        raise httpx.ConnectError("auth service unavailable", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unavailable), base_url="http://auth.internal"
    ) as client:
        assert await IntrospectionTokenVerifier(client).verify_token(issued.access_token) is None


async def test_existing_token_and_encrypted_state_restore_without_edge_signing_key(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    target = tmp_path / "copied"
    monkeypatch.setattr(fastmcp.settings, "home", source)
    state = {"valid": True}
    first = default_storage_provider(client_secret=MIGRATION_SECRET)
    await first.register_client(
        OAuthClientInformationFull(
            client_id="chatgpt",
            redirect_uris=[AnyUrl("https://client.example/callback")],
            grant_types=["authorization_code"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=SCOPE,
        )
    )
    existing_token = await issue(first, state)
    del first

    receipt = copy_with_receipt(
        kind="migration",
        source=source,
        target=target,
        receipt_path=tmp_path / "migration.receipt.json",
        lock_path=tmp_path / "migration.lock",
        signing_material=MIGRATION_SECRET,
        probe=StoppedWriters(),
    )
    assert receipt["source"] == str(source)
    assert receipt["target"] == str(target)
    assert receipt["source_manifest"]["tree_sha256"] != receipt["manifest"][
        "tree_sha256"
    ]
    assert receipt["source_manifest"]["logical_tree_sha256"] == receipt["manifest"][
        "logical_tree_sha256"
    ]

    monkeypatch.setattr(fastmcp.settings, "home", target)
    restored = default_storage_provider(client_secret=MIGRATION_SECRET)
    fake_github(restored, state)
    assert (await restored.get_client("chatgpt")).client_id == "chatgpt"
    transport = httpx.ASGITransport(app=introspection_app(restored))
    async with httpx.AsyncClient(transport=transport, base_url="http://auth.internal") as client:
        access = await IntrospectionTokenVerifier(client).verify_token(
            existing_token.access_token
        )
        assert access is not None and access.subject == USER_ID

    changed_key = default_storage_provider(
        client_secret="different-github-secret-material-over-32-bytes"
    )
    assert await changed_key.get_client("chatgpt") is None
