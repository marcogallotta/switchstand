import logging
import os
from collections.abc import Callable
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from sqlalchemy.ext.asyncio import create_async_engine

from .contracts import (
    AppendResult,
    ClosedModel,
    GroupedLookup,
    LaunchAuthority,
    RelatedLookup,
    SourceStoriesRequest,
    SourceStoriesResult,
    SourceStoryRequest,
    SourceStoryResult,
    SourceTaskRequest,
    SourceTaskResult,
    Status,
    WorkAppendRequest,
    WorkAttachmentsRequest,
    WorkAttachmentsResult,
    WorkEventRequest,
    WorkEventResult,
    WorkGetRequest,
    WorkHistoryRequest,
    WorkHistoryResult,
    WorkResult,
    WorkSearchItem,
)
from .core import Controller
from .grants import GrantedWorkResult
from .provider import AsanaProvider
from .state import PostgresState


class PublicWorkItem(WorkSearchItem):
    notes: str


class PublicCandidate(ClosedModel):
    title: str
    revision: str


class PublicRelated(ClosedModel):
    status: Literal["CANDIDATES", "UH_OH"]
    observed_revision: str | None = None
    candidates: tuple[PublicCandidate, ...] = ()
    complete: Literal[False] = False


class PublicReadGuard(ClosedModel):
    status: Literal["denied"] = "denied"
    operation: Literal["work_get"] = "work_get"
    reason: str
    next_action: str
    effect: Literal["not_sent"] = "not_sent"
    retry: Literal["none"] = "none"


class PublicWorkResult(ClosedModel):
    status: Status
    item: PublicWorkItem | None = None
    related: PublicRelated | None = None
    grouped: PublicRelated | None = None
    guard: PublicReadGuard | None = None


def project_work(result: WorkResult | GrantedWorkResult, include_related: bool) -> PublicWorkResult:
    """Present already-authorized work; never retrieve, authorize, or invent identities."""
    public = PublicWorkResult(status=result.status)
    if isinstance(result, GrantedWorkResult) and result.guard is not None:
        public.guard = PublicReadGuard(reason=result.guard.reason, next_action=result.guard.next_action)
    if result.status not in {"ok", "stale"}:
        return public
    if result.item is not None:
        item = result.item
        public.item = PublicWorkItem(id=item.id, title=item.title, notes=item.notes,
                                     completed=item.completed, revision=item.revision, routing=item.routing)
    if include_related:
        def related(value: RelatedLookup | GroupedLookup | None) -> PublicRelated | None:
            if value is None:
                return None
            return PublicRelated(status=value.status, observed_revision=value.observed_revision,
                                 candidates=tuple(PublicCandidate(title=c.title, revision=c.revision)
                                                  for c in value.candidates))
        public.related = related(result.related)
        public.grouped = related(result.grouped) if isinstance(result, WorkResult) else None
    return public


def controller_from_env() -> Controller:
    references = tuple(UUID(value) for value in os.getenv("REFERENCE_WORK_IDS", "").split(",") if value)
    authority = LaunchAuthority(active_work_id=UUID(os.environ["ACTIVE_WORK_ID"]), reference_work_ids=references)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    client = httpx.AsyncClient(base_url="https://app.asana.com/api/1.0", trust_env=False,
                               headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"})
    return Controller(authority, PostgresState(engine), {
        "asana": AsanaProvider(client, os.getenv("SWITCHSTAND_TEST_PROJECT_GID"))
    })


def closed_tool(
    server: MCPServer, name: str, function: Callable[..., Any],
    annotations: ToolAnnotations | None = None,
) -> None:
    server.tool(name=name, annotations=annotations)(function)
    tool = server._tool_manager.get_tool(name)  # pyright: ignore[reportPrivateUsage]
    assert tool is not None
    tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
    tool.fn_metadata.arg_model.model_rebuild(force=True)
    tool.parameters = tool.fn_metadata.arg_model.model_json_schema(by_alias=True)


def build_context_server(service: object, active_work_id: UUID) -> MCPServer:
    server = MCPServer("Switchstand read-only context")

    async def _work_get(
        api_version: Literal["1"], include_related: bool = False,
    ) -> PublicWorkResult:
        return project_work(await service.get(  # type: ignore[attr-defined]
            WorkGetRequest(
                api_version=api_version,
                work_id=active_work_id,
                include_related=include_related,
            )
        ), include_related)

    _work_get.__doc__ = (
        "Read the exact launch-bound work. Set include_related for bounded direct-child "
        "and grouped candidates; completeness is always unknown."
    )
    closed_tool(server, "work_get", _work_get, ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ))

    async def _work_history(
        api_version: Literal["1"], observed_revision: str,
        cursor: str | None = None, limit: int = 50,
    ) -> WorkHistoryResult:
        return await service.history(  # type: ignore[attr-defined]
            WorkHistoryRequest(
                api_version=api_version,
                work_id=active_work_id,
                observed_revision=observed_revision,
                cursor=cursor,
                limit=limit,
            )
        )

    _work_history.__doc__ = (
        "Read one revision-checked page of the exact launch-bound work history. "
        "Follow next_cursor until null; on stale, call work_get again and restart."
    )
    closed_tool(server, "work_history", _work_history, ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ))
    return server


def build_server(
    service: object, active_work_id: UUID, reference_work_ids: tuple[UUID, ...] = ()
) -> MCPServer:
    server = MCPServer("Switchstand")

    async def _work_get(
        api_version: Literal["1"], work_id: UUID | None = None, include_related: bool = False,
    ) -> PublicWorkResult:
        request = WorkGetRequest(api_version=api_version, work_id=work_id or active_work_id,
                                 include_related=include_related)
        return project_work(await service.get(request), include_related)  # type: ignore[attr-defined]

    references = ", ".join(map(str, reference_work_ids)) or "none"
    _work_get.__doc__ = (
        "Read launch-bound work. Set include_related for bounded direct-child and "
        "grouped candidates; completeness is always unknown. "
        "Omit work_id for the active assignment. "
        f"Bounded read-only reference WorkIds: {references}."
    )

    async def _work_history(
        api_version: Literal["1"], observed_revision: str, work_id: UUID | None = None,
        cursor: str | None = None, limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> WorkHistoryResult:
        """Read bounded history; on stale, repeat work_get and restart pagination."""
        return await service.history(  # type: ignore[attr-defined]
            WorkHistoryRequest(api_version=api_version, work_id=work_id or active_work_id,
                               observed_revision=observed_revision, cursor=cursor, limit=limit))

    async def _work_attachments(
        api_version: Literal["1"],
        observed_revision: Annotated[str, Field(min_length=1)],
        work_id: UUID | None = None,
        cursor: Annotated[str | None, Field(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50,
    ) -> WorkAttachmentsResult:
        """Read one bounded name-only attachment page; on stale, repeat work_get and restart."""
        return await service.attachments(  # type: ignore[attr-defined]
            WorkAttachmentsRequest(
                api_version=api_version, work_id=work_id or active_work_id,
                observed_revision=observed_revision, cursor=cursor, limit=limit,
            )
        )

    async def _work_event(
        api_version: Literal["1"], event_id: UUID, observed_revision: str,
        work_id: UUID | None = None,
    ) -> WorkEventResult:
        """Reread one opaque event at the observed work revision."""
        return await service.event(  # type: ignore[attr-defined]
            WorkEventRequest(api_version=api_version, work_id=work_id or active_work_id,
                             event_id=event_id, observed_revision=observed_revision))

    async def _source_task(api_version: Literal["1"], task_gid: str) -> SourceTaskResult:
        """Read one exact canonical Asana task by Asana task GID; this is not a WorkId."""
        return await service.source_task(  # type: ignore[attr-defined]
            SourceTaskRequest(api_version=api_version, task_gid=task_gid)
        )

    async def _source_stories(
        api_version: Literal["1"], task_gid: str, observed_revision: str,
        offset: str | None = None, limit: int = 50,
    ) -> SourceStoriesResult:
        """Read one revision-checked page of exact Asana task history/comments."""
        return await service.source_stories(  # type: ignore[attr-defined]
            SourceStoriesRequest(
                api_version=api_version,
                task_gid=task_gid,
                observed_revision=observed_revision,
                offset=offset,
                limit=limit,
            )
        )

    async def _source_story(
        api_version: Literal["1"], task_gid: str, story_gid: str,
        observed_revision: str,
    ) -> SourceStoryResult:
        """Reread one exact material Asana story and verify its task and task revision."""
        return await service.source_story(  # type: ignore[attr-defined]
            SourceStoryRequest(
                api_version=api_version,
                task_gid=task_gid,
                story_gid=story_gid,
                observed_revision=observed_revision,
            )
        )

    async def _work_append(api_version: Literal["1"], work_id: UUID, text: str) -> AppendResult:
        """Append one history entry to the active work item and return exact Asana effect identity."""
        return await service.append(WorkAppendRequest(api_version=api_version, work_id=work_id, text=text))  # type: ignore[attr-defined]

    closed_tool(server, "work_get", _work_get)
    closed_tool(server, "work_attachments", _work_attachments)
    closed_tool(server, "work_history", _work_history)
    closed_tool(server, "work_event", _work_event)
    closed_tool(server, "source_task", _source_task)
    closed_tool(server, "source_stories", _source_stories)
    closed_tool(server, "source_story", _source_story)
    closed_tool(server, "work_append", _work_append)
    return server


def server_from_env() -> MCPServer:
    if os.getenv("SWITCHSTAND_MANAGED") != "1":
        return MCPServer("Switchstand (unbound)")
    service = controller_from_env()
    return build_server(
        service, service.authority.active_work_id, service.authority.reference_work_ids
    )


def protect_provider_logs() -> None:
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers[:] = [logging.NullHandler()]
        logger.propagate = False


def main() -> None:
    protect_provider_logs()
    server_from_env().run()


if __name__ == "__main__":
    main()
