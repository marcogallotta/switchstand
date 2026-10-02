"""Default-off authenticated ChatGPT MCP HTTP edge."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import AnyHttpUrl
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .canonical_relations import CanonicalRelationsRepository
from .canonical_work import CanonicalWorkRepository
from .canonical_work_runtime import CanonicalWorkRuntime
from .chatgpt import ChatGPTService
from .chatgpt_mcp import build_ordinary_tools, ordinary_tool_annotations
from .grant_state import GrantState
from .lifecycle import LifecycleRepository, RequiredResultPersistence
from .messages import MessageState
from .oauth_continuity import (
    FASTMCP_ACCESS_TOKEN_LIFETIME_SECONDS,
    SwitchstandGitHubProvider,
)
from .principal import RequestPrincipal
from .provider import AsanaProvider
from .stable_auth import (
    REQUIRED_SCOPE,
    IntrospectionTokenVerifier,
    delegated_auth,
    normalize_resource_url,
)
from .state import PostgresState

LOG = logging.getLogger(__name__)
CERTIFICATION_RUNTIME_PATH = "/.well-known/switchstand-certification-runtime"
GRACEFUL_SHUTDOWN_SECONDS = 30
_https_resource_url = normalize_resource_url


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


def http_middleware() -> list[Middleware]:
    return [Middleware(_CompleteMCPStream)]


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
            **(values | {"resource_url": normalize_resource_url(values["resource_url"])}),
            bind_host=_loopback_host(os.getenv("SWITCHSTAND_MCP_BIND_HOST", "127.0.0.1")),
            bind_port=_bind_port(os.getenv("SWITCHSTAND_MCP_BIND_PORT", "8790")),
        )


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


def runtime_identity_from_meta(meta: dict[str, Any]) -> str:
    """Read one supported host identity without conflating host namespaces."""
    present = [(key, meta[key]) for key in ("openai/session", "threadId") if key in meta]
    if len(present) != 1 or not isinstance(present[0][1], str) or not present[0][1]:
        return ""
    key, value = present[0]
    return value if key == "openai/session" else f"codex:{value}"


def _runtime_identity() -> str:
    """Read the stable host identity from the current MCP call metadata."""
    request_context = get_context().request_context
    if request_context is None:
        return ""
    meta = request_context.meta
    if meta is None:
        return ""
    return runtime_identity_from_meta(meta)


def create_app(
    service: ChatGPTService, config: MCPAuthConfig, *, client_storage: Any | None = None,
    certification_runtime: tuple[str, str] | None = None,
):
    """Build the current combined OAuth/resource application."""
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
    return _create_resource_app(
        service,
        issuer_url=config.issuer_url,
        resource_url=config.resource_url,
        auth=auth,
        certification_runtime=certification_runtime,
    )


def create_delegated_app(
    service: ChatGPTService,
    verifier: IntrospectionTokenVerifier,
    *,
    certification_runtime: tuple[str, str] | None = None,
):
    """Build an inert resource edge with no GitHub or OAuth signing state."""
    return _create_resource_app(
        service,
        issuer_url=verifier.contract.issuer_url,
        resource_url=verifier.contract.resource_url,
        auth=delegated_auth(verifier),
        certification_runtime=certification_runtime,
    )


def _create_resource_app(
    service: ChatGPTService,
    *,
    issuer_url: str,
    resource_url: str,
    auth: Any,
    certification_runtime: tuple[str, str] | None,
):
    service = ChatGPTService(
        RequestPrincipal(issuer_url, resource_url, REQUIRED_SCOPE),
        service.state,
        service.grants,
        service.providers,
        service.messages,
        service.required_results,
        ordinary_workspace_admission=True,
        canonical_work=service.canonical_work,
        canonical_work_active=service.canonical_work_active,
    )
    server = FastMCP("Switchstand ChatGPT", version="1", auth=auth)
    for name, tool in build_ordinary_tools(
        service, _audit,
        agent_identity=_runtime_identity,
    ):
        server.tool(tool, annotations=ordinary_tool_annotations(name))
    app = server.http_app(
        path="/mcp", json_response=True, stateless_http=False,
        middleware=http_middleware(),
    )
    if certification_runtime is not None:
        runtime_sha, run_id = certification_runtime

        async def certification_readback(_request: Request) -> JSONResponse:
            return JSONResponse({"runtime_sha": runtime_sha, "run_id": run_id})

        app.add_route(CERTIFICATION_RUNTIME_PATH, certification_readback, methods=["GET"])
    return app


@asynccontextmanager
async def resource_service() -> AsyncGenerator[tuple[ChatGPTService, tuple[str, str] | None]]:
    """Own the resource edge's provider/database dependencies for either launch mode."""
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
        canonical_work = CanonicalWorkRuntime(
            CanonicalWorkRepository(engine), CanonicalRelationsRepository(engine)
        )
        service = ChatGPTService(unresolved_principal, PostgresState(engine), grants, {
            "asana": provider,
        }, MessageState(engine, grants), RequiredResultPersistence(LifecycleRepository(engine)),
            canonical_work=canonical_work)
        runtime = None
        if marker:
            runtime = (
                os.environ["SWITCHSTAND_CERTIFICATION_RUNTIME_SHA"],
                os.environ["SWITCHSTAND_CERTIFICATION_RUN_ID"],
            )
        yield service, runtime
    finally:
        await client.aclose()
        await engine.dispose()


async def serve() -> None:
    config = MCPAuthConfig.from_environment()
    async with resource_service() as (service, runtime):
        app = create_app(service, config, certification_runtime=runtime)
        await app.state.fastmcp_server.run_http_async(
            host=config.bind_host, port=config.bind_port, path="/mcp",
            json_response=True, stateless_http=False, show_banner=False,
            uvicorn_config={"timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_SECONDS},
            middleware=http_middleware(),
        )


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
