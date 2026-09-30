"""Inert single-host authorization service and private token introspection."""

from __future__ import annotations

import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlparse

import httpx
from fastmcp.server.auth import RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .oauth_continuity import (
    FASTMCP_ACCESS_TOKEN_LIFETIME_SECONDS,
    SwitchstandGitHubProvider,
)

INTROSPECTION_PATH = "/internal/oauth/verify"
INTROSPECTION_CLOCK_SKEW_SECONDS = 5
MAX_INTROSPECTION_RESPONSE_BYTES = 4096
REQUIRED_SCOPE = "read:user"


def normalize_resource_url(raw_value: str) -> str:
    value = raw_value.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("resource URL must be an absolute HTTPS URL")
    if not parsed.path.endswith("/mcp") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("resource URL must end exactly in /mcp")
    return str(AnyHttpUrl(value))


@dataclass(frozen=True, slots=True)
class IntrospectionContract:
    """Exact principal fields accepted by a replaceable MCP resource edge."""

    issuer_url: str
    resource_url: str
    github_user_id: str
    scopes: tuple[str, ...] = field(default=(REQUIRED_SCOPE,), init=False)
    clock_skew_seconds: int = INTROSPECTION_CLOCK_SKEW_SECONDS

    @classmethod
    def for_resource(
        cls,
        resource_url: str,
        github_user_id: str,
        *,
        clock_skew_seconds: int = INTROSPECTION_CLOCK_SKEW_SECONDS,
    ) -> IntrospectionContract:
        normalized = normalize_resource_url(resource_url)
        issuer = str(AnyHttpUrl(normalized.removesuffix("/mcp").rstrip("/")))
        return cls(issuer, normalized, github_user_id, clock_skew_seconds=clock_skew_seconds)

    def __post_init__(self) -> None:
        normalized = normalize_resource_url(self.resource_url)
        expected_issuer = str(AnyHttpUrl(normalized.removesuffix("/mcp").rstrip("/")))
        if self.resource_url != normalized or self.issuer_url != expected_issuer:
            raise ValueError("introspection issuer and resource must be exact and normalized")
        if not self.github_user_id.isdigit():
            raise ValueError("GitHub user ID must be numeric")
        if type(self.clock_skew_seconds) is not int or self.clock_skew_seconds < 0:
            raise ValueError("clock skew must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class StableAuthConfig:
    """Secrets and identity state owned only by the stable auth service."""

    github_client_id: str
    github_client_secret: str = field(repr=False)
    internal_secret: str = field(repr=False)
    contract: IntrospectionContract

    def __post_init__(self) -> None:
        if not self.github_client_id.strip() or not self.github_client_secret.strip():
            raise ValueError("GitHub OAuth credentials must be nonempty")
        if not self.internal_secret.strip():
            raise ValueError("private introspection secret must be nonempty")


def create_auth_service(
    config: StableAuthConfig,
    *,
    client_storage: Any | None = None,
) -> Starlette:
    """Create public OAuth routes plus one private fail-closed introspection route."""
    auth_options: dict[str, Any] = {}
    if client_storage is not None:
        auth_options["client_storage"] = client_storage
    provider = SwitchstandGitHubProvider(
        client_id=config.github_client_id,
        client_secret=config.github_client_secret,
        allowed_user_id=config.contract.github_user_id,
        base_url=config.contract.issuer_url,
        issuer_url=config.contract.issuer_url,
        required_scopes=list(config.contract.scopes),
        require_authorization_consent=True,
        fastmcp_access_token_expiry_seconds=FASTMCP_ACCESS_TOKEN_LIFETIME_SECONDS,
        **auth_options,
    )

    async def verify(request: Request) -> Response:
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {config.internal_secret}"
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return Response(status_code=401)
        try:
            raw_payload = await request.json()
            payload = (
                cast(dict[str, object], raw_payload)
                if isinstance(raw_payload, dict)
                else {}
            )
            token = payload.get("token")
            if not isinstance(token, str) or not token:
                return Response(status_code=400)
            access = await provider.verify_token(token)
        except Exception:  # noqa: BLE001 -- private trust boundary fails closed
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

    app = Starlette(
        routes=[
            *provider.get_routes(mcp_path="/mcp"),
            Route(INTROSPECTION_PATH, verify, methods=["POST"]),
        ]
    )
    app.state.auth_provider = provider
    return app


class IntrospectionTokenVerifier(TokenVerifier):
    """Validate a stable auth service's bounded principal on every request."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        internal_secret: str,
        contract: IntrospectionContract,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(required_scopes=list(contract.scopes))
        if not internal_secret.strip():
            raise ValueError("private introspection secret must be nonempty")
        self.client = client
        self.internal_secret = internal_secret
        self.contract = contract
        self.clock = clock

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        try:
            response = await self.client.post(
                INTROSPECTION_PATH,
                headers={"Authorization": f"Bearer {self.internal_secret}"},
                json={"token": token},
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200 or len(response.content) > MAX_INTROSPECTION_RESPONSE_BYTES:
            return None
        try:
            raw_payload = response.json()
        except ValueError:
            return None
        if not isinstance(raw_payload, dict):
            return None
        payload = cast(dict[str, object], raw_payload)
        client_id = payload.get("client_id")
        expires_at = payload.get("expires_at")
        scopes = payload.get("scopes")
        if (
            set(payload) != {"client_id", "scopes", "subject", "expires_at", "resource", "issuer"}
            or payload.get("subject") != self.contract.github_user_id
            or payload.get("issuer") != self.contract.issuer_url
            or payload.get("resource") != self.contract.resource_url
            or not isinstance(client_id, str)
            or not client_id.strip()
            or type(expires_at) is not int
            or expires_at <= int(self.clock()) + self.contract.clock_skew_seconds
            or not isinstance(scopes, list)
            or scopes != list(self.contract.scopes)
        ):
            return None
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=cast(list[str], scopes),
            expires_at=expires_at,
            subject=self.contract.github_user_id,
            resource=self.contract.resource_url,
            claims={"iss": self.contract.issuer_url},
        )


def delegated_auth(
    verifier: IntrospectionTokenVerifier,
) -> RemoteAuthProvider:
    """Build resource-server metadata without any OAuth signing/provider state."""
    contract = verifier.contract
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(contract.issuer_url)],
        base_url=contract.issuer_url,
        scopes_supported=list(contract.scopes),
    )
