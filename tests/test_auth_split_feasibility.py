import time

import fastmcp
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy.models import ClientCode
from fastmcp.server.auth.providers.jwt import JWTVerifier
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AuthorizationCode, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl, AnyUrl
from starlette.applications import Starlette
from starlette.testclient import TestClient

from switchstand.oauth_continuity import SwitchstandGitHubProvider

ISSUER = "https://switchstand.example.com/"
RESOURCE = "https://switchstand.example.com/mcp"
USER_ID = "192548"
SCOPE = "read:user"
SIGNING_KEY = b"disposable-auth-split-signing-key"


class IdentityEmbeddingProvider(SwitchstandGitHubProvider):
    """Make the already-validated upstream identity available to a remote edge."""

    async def _extract_upstream_claims(self, idp_tokens):
        validated = await self._token_validator.verify_token(idp_tokens["access_token"])
        if (
            validated is None
            or validated.subject != self.allowed_user_id
            or not self._required_upstream_scopes.issubset(validated.scopes)
        ):
            raise TokenError("invalid_grant", "GitHub identity is not admitted")
        return {
            "github_user_id": validated.subject,
            "github_scopes": sorted(validated.scopes),
        }


class IdentityBoundJWTVerifier(JWTVerifier):
    """Require the signed upstream identity as well as standard JWT claims."""

    def __init__(self, *, allowed_user_id, **kwargs):
        self.allowed_user_id = allowed_user_id
        super().__init__(**kwargs)

    async def verify_token(self, token):
        access = await super().verify_token(token)
        if access is None:
            return None
        identity = access.claims.get("upstream_claims")
        if not isinstance(identity, dict):
            return None
        scopes = identity.get("github_scopes")
        if (
            identity.get("github_user_id") != self.allowed_user_id
            or not isinstance(scopes, list)
            or SCOPE not in scopes
        ):
            return None
        return access.model_copy(
            update={
                "subject": self.allowed_user_id,
                "resource": access.claims.get("aud"),
            }
        )


def auth_provider(*, storage=None, signing_key=SIGNING_KEY):
    storage = storage or MemoryStore()
    subject = IdentityEmbeddingProvider(
        client_id="github-client",
        client_secret="github-secret",
        allowed_user_id=USER_ID,
        base_url=ISSUER,
        resource_base_url=ISSUER,
        issuer_url=ISSUER,
        required_scopes=[SCOPE],
        client_storage=storage,
        jwt_signing_key=signing_key,
    )
    subject.get_routes(mcp_path="/mcp")
    return subject


def default_storage_provider(*, signing_key=SIGNING_KEY):
    subject = IdentityEmbeddingProvider(
        client_id="github-client",
        client_secret="github-secret",
        allowed_user_id=USER_ID,
        base_url=ISSUER,
        resource_base_url=ISSUER,
        issuer_url=ISSUER,
        required_scopes=[SCOPE],
        jwt_signing_key=signing_key,
    )
    subject.get_routes(mcp_path="/mcp")
    return subject


def edge_auth(signing_key=SIGNING_KEY, *, audience=RESOURCE):
    verifier = IdentityBoundJWTVerifier(
        allowed_user_id=USER_ID,
        public_key=signing_key,
        algorithm="HS256",
        issuer=ISSUER,
        audience=audience,
        required_scopes=[SCOPE],
    )
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(ISSUER)],
        base_url=ISSUER,
        scopes_supported=[SCOPE],
    )


def fake_github(subject, *, user_id=USER_ID, scopes=(SCOPE,)):
    async def verify(token):
        if token != "github-token":
            return None
        return AccessToken(
            token=token,
            client_id="github",
            scopes=list(scopes),
            subject=user_id,
        )

    subject._token_validator.verify_token = verify


async def issue(subject, client_id="chatgpt"):
    fake_github(subject)
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
    resource = edge_auth()
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
    assert protected["bearer_methods_supported"] == ["header"]


async def test_separate_edge_requires_resource_scope_and_signed_github_identity():
    authorization = auth_provider()
    issued = await issue(authorization)
    access = await edge_auth().verify_token(issued.access_token)
    assert access is not None
    assert (access.subject, access.client_id, access.resource) == (
        USER_ID,
        "chatgpt",
        RESOURCE,
    )

    assert await edge_auth(audience="https://wrong.example/mcp").verify_token(
        issued.access_token
    ) is None
    missing_scope = authorization.jwt_issuer.issue_access_token(
        client_id="chatgpt",
        scopes=[],
        jti="missing-scope",
        expires_in=60,
        upstream_claims={"github_user_id": USER_ID, "github_scopes": [SCOPE]},
    )
    assert await edge_auth().verify_token(missing_scope) is None
    wrong_identity = authorization.jwt_issuer.issue_access_token(
        client_id="chatgpt",
        scopes=[SCOPE],
        jti="wrong-user",
        expires_in=60,
        upstream_claims={"github_user_id": "999999", "github_scopes": [SCOPE]},
    )
    assert await edge_auth().verify_token(wrong_identity) is None

    legacy_token = authorization.jwt_issuer.issue_access_token(
        client_id="existing-chat",
        scopes=[SCOPE],
        jti="legacy-with-server-side-identity-only",
        expires_in=60,
    )
    assert await edge_auth().verify_token(legacy_token) is None


async def test_encrypted_state_and_tokens_restore_only_with_the_same_key(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(fastmcp.settings, "home", tmp_path)
    first = default_storage_provider()
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
    issued = await issue(first)
    del first

    restored = default_storage_provider()
    fake_github(restored)
    assert (await restored.get_client("chatgpt")).client_id == "chatgpt"
    assert (await restored.verify_token(issued.access_token)).subject == USER_ID
    assert (await edge_auth().verify_token(issued.access_token)).subject == USER_ID

    changed_key = default_storage_provider(signing_key=b"different-disposable-key-material")
    assert await changed_key.get_client("chatgpt") is None
    assert await IdentityBoundJWTVerifier(
        allowed_user_id=USER_ID,
        public_key=b"different-disposable-key-material",
        algorithm="HS256",
        issuer=ISSUER,
        audience=RESOURCE,
        required_scopes=[SCOPE],
    ).verify_token(issued.access_token) is None
