"""Default-off authenticated ChatGPT MCP HTTP edge."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_context
from joserfc.errors import JoseError
from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import AnyHttpUrl
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .chatgpt import ChatGPTService
from .chatgpt_mcp import build_ordinary_tools, ordinary_tool_annotations
from .grant_state import GrantState
from .lifecycle import LifecycleRepository, RequiredResultPersistence
from .messages import MessageState
from .principal import RequestPrincipal
from .provider import AsanaProvider
from .state import PostgresState

LOG = logging.getLogger(__name__)
REQUIRED_SCOPE = "read:user"
CERTIFICATION_RUNTIME_PATH = "/.well-known/switchstand-certification-runtime"
GRACEFUL_SHUTDOWN_SECONDS = 30
FASTMCP_ACCESS_TOKEN_LIFETIME_SECONDS = 365 * 24 * 60 * 60


class _CompleteMCPStream:
    """Finish a normally-returning MCP GET stream at the ASGI boundary.

    sse-starlette's signal watcher cancels its body writer before the final
    chunk. Keep this integration shim until that dependency completes the
    response itself; exceptions still propagate without being disguised.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "GET" or scope["path"] != "/mcp":
            await self.app(scope, receive, send)
            return

        response_started = False
        response_complete = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_started, response_complete
            if message["type"] == "http.response.start":
                response_started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete = True
            await send(message)

        await self.app(scope, receive, tracked_send)
        if response_started and not response_complete:
            await send({"type": "http.response.body", "body": b"", "more_body": False})


def _http_middleware() -> list[Middleware]:
    return [Middleware(_CompleteMCPStream)]


def _https_resource_url(raw_value: str) -> str:
    value = raw_value.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("SWITCHSTAND_MCP_RESOURCE_URL must be an absolute HTTPS URL")
    if not parsed.path.endswith("/mcp") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("SWITCHSTAND_MCP_RESOURCE_URL must end exactly in /mcp")
    return str(AnyHttpUrl(value))


def _loopback_host(raw_value: str) -> str:
    value = raw_value.strip()
    if value not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("SWITCHSTAND_MCP_BIND_HOST must remain loopback-only")
    return value


def _bind_port(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("SWITCHSTAND_MCP_BIND_PORT must be an integer") from exc
    if not 1 <= value <= 65535:
        raise ValueError("SWITCHSTAND_MCP_BIND_PORT must be between 1 and 65535")
    return value


@dataclass(frozen=True, slots=True)
class MCPAuthConfig:
    github_client_id: str
    github_client_secret: str
    github_user_id: str
    resource_url: str
    bind_host: str = "127.0.0.1"
    bind_port: int = 8790

    @property
    def base_url(self) -> str:
        return str(AnyHttpUrl(self.resource_url.removesuffix("/mcp").rstrip("/")))

    @property
    def issuer_url(self) -> str:
        return self.base_url

    @classmethod
    def from_environment(cls) -> MCPAuthConfig:
        values = {
            "github_client_id": os.getenv("SWITCHSTAND_MCP_GITHUB_CLIENT_ID", "").strip(),
            "github_client_secret": os.getenv("SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET", "").strip(),
            "github_user_id": os.getenv("SWITCHSTAND_MCP_GITHUB_USER_ID", "").strip(),
            "resource_url": os.getenv("SWITCHSTAND_MCP_RESOURCE_URL", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(f"required MCP OAuth configuration missing: {', '.join(missing)}")
        if not values["github_user_id"].isdigit():
            raise ValueError("SWITCHSTAND_MCP_GITHUB_USER_ID must be a numeric GitHub user ID")
        return cls(
            **(values | {"resource_url": _https_resource_url(values["resource_url"])}),
            bind_host=_loopback_host(os.getenv("SWITCHSTAND_MCP_BIND_HOST", "127.0.0.1")),
            bind_port=_bind_port(os.getenv("SWITCHSTAND_MCP_BIND_PORT", "8790")),
        )


class SwitchstandGitHubProvider(GitHubProvider):
    """GitHub proxy restricted to one user and bridged to RequestPrincipal."""

    def __init__(self, *, allowed_user_id: str, **kwargs: Any) -> None:
        self.allowed_user_id = allowed_user_id
        super().__init__(**kwargs)

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None or access.subject != self.allowed_user_id:
            if access is not None:
                LOG.warning("mcp_github_user_rejected")
            return None
        try:
            claims = self.jwt_issuer.verify_token(token)
        except JoseError:
            return None
        scope = claims.get("scope")
        client_id = claims.get("client_id")
        issuer = claims.get("iss")
        resource = claims.get("aud")
        expires_at = claims.get("exp")
        if (not isinstance(scope, str) or REQUIRED_SCOPE not in scope.split()
                or not isinstance(client_id, str) or not client_id
                or not isinstance(issuer, str) or not isinstance(resource, str)
                or not isinstance(expires_at, int)):
            return None
        bridged_claims = dict(access.claims or {}) | {"iss": issuer}
        return access.model_copy(update={
            "client_id": client_id,
            "scopes": scope.split(),
            "expires_at": expires_at,
            "resource": resource,
            "claims": bridged_claims,
        })


def _audit(tool: str, target: str | None, status: str) -> None:
    token = get_access_token()
    LOG.info(
        "chatgpt_mcp tool=%s issuer=%s subject=%s client_id=%s target=%s status=%s",
        tool,
        None if token is None else (token.claims or {}).get("iss"),
        None if token is None else token.subject,
        None if token is None else token.client_id,
        target,
        status,
    )


def _openai_session() -> str:
    """Read ChatGPT's stable chat identity from the current MCP call metadata."""
    request_context = get_context().request_context
    if request_context is None:
        return ""
    meta = request_context.meta
    if meta is None:
        return ""
    value = meta.get("openai/session")
    return value if isinstance(value, str) else ""


def create_app(
    service: ChatGPTService, config: MCPAuthConfig, *, client_storage: Any | None = None,
    certification_runtime: tuple[str, str] | None = None,
):
    """Build the inert-until-called authenticated HTTP application."""
    service = ChatGPTService(
        RequestPrincipal(config.issuer_url, config.resource_url, REQUIRED_SCOPE),
        service.state,
        service.grants,
        service.providers,
        service.messages,
        service.required_results,
        ordinary_workspace_admission=True,
    )
    auth_options: dict[str, Any] = {}
    if client_storage is not None:
        auth_options["client_storage"] = client_storage
    auth = SwitchstandGitHubProvider(
        client_id=config.github_client_id,
        client_secret=config.github_client_secret,
        allowed_user_id=config.github_user_id,
        base_url=config.base_url,
        issuer_url=config.issuer_url,
        required_scopes=[REQUIRED_SCOPE],
        require_authorization_consent=True,
        fastmcp_access_token_expiry_seconds=FASTMCP_ACCESS_TOKEN_LIFETIME_SECONDS,
        **auth_options,
    )
    server = FastMCP("Switchstand ChatGPT", version="1", auth=auth)
    for name, tool in build_ordinary_tools(
        service, _audit,
        session_generation=lambda: get_context().session_id,
        agent_identity=_openai_session,
    ):
        server.tool(tool, annotations=ordinary_tool_annotations(name))
    app = server.http_app(
        path="/mcp", json_response=True, stateless_http=False,
        middleware=_http_middleware(),
    )
    if certification_runtime is not None:
        runtime_sha, run_id = certification_runtime

        async def certification_readback(_request: Request) -> JSONResponse:
            return JSONResponse({"runtime_sha": runtime_sha, "run_id": run_id})

        app.add_route(CERTIFICATION_RUNTIME_PATH, certification_readback, methods=["GET"])
    return app


async def serve() -> None:
    config = MCPAuthConfig.from_environment()
    engine = create_async_engine(os.environ["DATABASE_URL"])
    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", trust_env=False,
        headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"},
    )
    try:
        async def unresolved_principal():
            return None

        test_project = os.getenv("SWITCHSTAND_TEST_PROJECT_GID", "").strip()
        marker = os.getenv("SWITCHSTAND_CERTIFICATION_FIXTURE_MARKER", "").strip()
        provider = AsanaProvider(
            client, test_project or None, test_only=bool(test_project),
            create_notes_suffix=marker or None,
        )
        grants = GrantState(engine)
        service = ChatGPTService(unresolved_principal, PostgresState(engine), grants, {
            "asana": provider,
        }, MessageState(engine, grants), RequiredResultPersistence(LifecycleRepository(engine)))
        runtime = None
        if marker:
            runtime = (
                os.environ["SWITCHSTAND_CERTIFICATION_RUNTIME_SHA"],
                os.environ["SWITCHSTAND_CERTIFICATION_RUN_ID"],
            )
        app = create_app(service, config, certification_runtime=runtime)
        await app.state.fastmcp_server.run_http_async(
            host=config.bind_host, port=config.bind_port, path="/mcp",
            json_response=True, stateless_http=False, show_banner=False,
            uvicorn_config={"timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_SECONDS},
            middleware=_http_middleware(),
        )
    finally:
        await client.aclose()
        await engine.dispose()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
