"""Default-off authenticated ChatGPT MCP HTTP edge."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from uuid import UUID

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider
from joserfc.errors import JoseError
from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import AnyHttpUrl, Field
from sqlalchemy.ext.asyncio import create_async_engine

from .chatgpt import ChatGPTService
from .contracts import (
    SourceStoriesRequest,
    SourceStoriesResult,
    SourceStoryRequest,
    SourceStoryResult,
    SourceTaskRequest,
    SourceTaskResult,
    WorkAttachmentsRequest,
    WorkAttachmentsResult,
    WorkEventRequest,
    WorkEventResult,
    WorkHistoryRequest,
    WorkHistoryResult,
    WorkSearchRequest,
    WorkSearchResult,
)
from .grant_state import GrantState
from .grants import GrantResult, GuardOutcome, ProtectedAppend, ProtectedCreate
from .mcp import PublicWorkResult, project_work
from .principal import RequestPrincipal
from .provider import AsanaProvider
from .state import PostgresState
from .test_create_provider import TestCreateAsanaProvider

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
    ) -> PublicWorkResult:
        result = await service.get(work_id, include_related=include_related)
        _audit("work_get", None if work_id is None else str(work_id), result.status)
        return project_work(result, include_related)

    async def work_search(
        api_version: Literal["1"], text: str | None = None,
        completed: bool | None = None, cursor: str | None = None, limit: int = 50,
    ) -> WorkSearchResult:
        result = await service.search(WorkSearchRequest(
            api_version=api_version, text=text, completed=completed,
            cursor=cursor, limit=limit,
        ))
        _audit("work_search", None, result.status)
        return result

    async def work_history(
        api_version: Literal["1"], work_id: UUID, observed_revision: str,
        cursor: str | None = None, limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> WorkHistoryResult:
        """Read bounded history; on stale, repeat work_get and restart pagination."""
        result = await service.history(
            WorkHistoryRequest(api_version=api_version, work_id=work_id,
                               observed_revision=observed_revision, cursor=cursor, limit=limit))
        _audit("work_history", str(work_id), result.status)
        return result

    async def work_attachments(
        api_version: Literal["1"], work_id: UUID, observed_revision: str,
        cursor: Annotated[str | None, Field(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50,
    ) -> WorkAttachmentsResult:
        """List attachment names only; on stale, repeat work_get and restart pagination."""
        result = await service.attachments(WorkAttachmentsRequest(
            api_version=api_version, work_id=work_id, observed_revision=observed_revision,
            cursor=cursor, limit=limit,
        ))
        _audit("work_attachments", str(work_id), result.status)
        return result

    async def work_event(
        api_version: Literal["1"], event_id: UUID, observed_revision: str,
        work_id: UUID,
    ) -> WorkEventResult:
        """Reread one opaque event at the observed work revision."""
        result = await service.event(
            WorkEventRequest(api_version=api_version, work_id=work_id,
                             event_id=event_id, observed_revision=observed_revision))
        _audit("work_event", str(work_id), result.status)
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

    async def work_create(
        api_version: Literal["1"], operation_id: UUID, parent_work_id: UUID,
        grant_version: int, title: str, notes: str = "",
    ) -> GuardOutcome:
        result = await service.create(ProtectedCreate(
            api_version=api_version, operation_id=operation_id,
            parent_work_id=parent_work_id, grant_version=grant_version,
            title=title, notes=notes,
        ))
        _audit("work_create", str(parent_work_id), result.status)
        return result

    for tool in (grant_get, work_get, work_search, work_history, work_attachments, work_event, source_task, source_stories, source_story,
                 work_append, work_create):
        server.tool(tool)
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

        test_project = os.getenv("SWITCHSTAND_TEST_PROJECT_GID", "").strip()
        correlation_field = os.getenv("SWITCHSTAND_CREATE_CORRELATION_FIELD_GID", "").strip()
        if test_project and correlation_field:
            provider = TestCreateAsanaProvider(client, test_project, correlation_field)
        else:
            provider = AsanaProvider(client, test_project or None, test_only=bool(test_project))
        service = ChatGPTService(unresolved_principal, PostgresState(engine), GrantState(engine), {
            "asana": provider,
        })
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
