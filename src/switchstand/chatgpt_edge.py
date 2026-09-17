"""Default-off authenticated ChatGPT MCP HTTP edge."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider
from joserfc.errors import JoseError
from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import AnyHttpUrl
from sqlalchemy.ext.asyncio import create_async_engine

from .chatgpt import ChatGPTService, RequiredResultSaveRequest
from .contracts import (
    SourceStoriesRequest,
    SourceStoriesResult,
    SourceStoryRequest,
    SourceStoryResult,
    SourceTaskRequest,
    SourceTaskResult,
)
from .grant_state import GrantState
from .grants import GrantedWorkResult, GrantResult, GuardOutcome, ProtectedAppend
from .lifecycle import LifecycleRepository, RequiredResultPersistence
from .principal import RequestPrincipal
from .provider import AsanaProvider
from .state import PostgresState

LOG = logging.getLogger(__name__)
REQUIRED_SCOPE = "read:user"


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


def create_app(
    service: ChatGPTService, config: MCPAuthConfig, *, client_storage: Any | None = None,
):
    """Build the inert-until-called authenticated HTTP application."""
    service = ChatGPTService(
        RequestPrincipal(config.issuer_url, config.resource_url, REQUIRED_SCOPE),
        service.state,
        service.grants,
        service.providers,
        required_results=service.required_results,
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
        **auth_options,
    )
    server = FastMCP("Switchstand ChatGPT", version="1", auth=auth)

    async def grant_get(api_version: Literal["1"]) -> GrantResult:
        result = await service.grant_get()
        _audit("grant_get", None, result.status)
        return result

    async def work_get(
        api_version: Literal["1"], work_id: UUID | None = None, include_related: bool = False,
    ) -> GrantedWorkResult:
        result = await service.get(work_id, include_related=include_related)
        _audit("work_get", None if work_id is None else str(work_id), result.status)
        return result

    async def source_task(api_version: Literal["1"], task_gid: str) -> SourceTaskResult:
        result = await service.source_task(SourceTaskRequest(api_version=api_version, task_gid=task_gid))
        _audit("source_task", task_gid, result.status)
        return result

    async def source_stories(
        api_version: Literal["1"], task_gid: str, observed_revision: str,
        offset: str | None = None, limit: int = 50,
    ) -> SourceStoriesResult:
        result = await service.source_stories(SourceStoriesRequest(
            api_version=api_version, task_gid=task_gid, observed_revision=observed_revision,
            offset=offset, limit=limit,
        ))
        _audit("source_stories", task_gid, result.status)
        return result

    async def source_story(
        api_version: Literal["1"], task_gid: str, story_gid: str, observed_revision: str,
    ) -> SourceStoryResult:
        result = await service.source_story(SourceStoryRequest(
            api_version=api_version, task_gid=task_gid, story_gid=story_gid,
            observed_revision=observed_revision,
        ))
        _audit("source_story", task_gid, result.status)
        return result

    async def work_append(
        api_version: Literal["1"], operation_id: UUID, work_id: UUID,
        grant_version: int, observed_revision: str, text: str,
    ) -> GuardOutcome:
        result = await service.append(ProtectedAppend(
            api_version=api_version,
            operation_id=operation_id,
            work_id=work_id,
            grant_version=grant_version,
            observed_revision=observed_revision,
            text=text,
        ))
        _audit("work_append", str(work_id), result.status)
        return result

    async def required_result_save(
        api_version: Literal["1"], work_id: UUID, grant_version: int,
        observed_revision: str, text: str,
    ) -> GuardOutcome:
        """Save one required result; operation identity is server-owned."""
        result = await service.required_result_save(RequiredResultSaveRequest(
            api_version=api_version,
            work_id=work_id,
            grant_version=grant_version,
            observed_revision=observed_revision,
            text=text,
        ))
        _audit("required_result_save", str(work_id), result.status)
        return result

    for tool in (grant_get, work_get, source_task, source_stories, source_story, work_append):
        server.tool(tool)
    if service.required_results is not None:
        server.tool(required_result_save)
    return server.http_app(path="/mcp", json_response=True, stateless_http=True)


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

        required_results = RequiredResultPersistence(LifecycleRepository(engine))
        service = ChatGPTService(unresolved_principal, PostgresState(engine), GrantState(engine), {
            "asana": AsanaProvider(client, os.getenv("SWITCHSTAND_TEST_PROJECT_GID")),
        }, required_results=required_results)
        app = create_app(service, config)
        await app.state.fastmcp_server.run_http_async(
            host=config.bind_host, port=config.bind_port, path="/mcp",
            json_response=True, stateless_http=True, show_banner=False,
        )
    finally:
        await client.aclose()
        await engine.dispose()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
