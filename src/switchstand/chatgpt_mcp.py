from typing import Literal
from uuid import UUID

from mcp.server import MCPServer

from .chatgpt import ChatGPTService
from .contracts import (
    SourceStoriesRequest,
    SourceStoriesResult,
    SourceStoryRequest,
    SourceStoryResult,
    SourceTaskRequest,
    SourceTaskResult,
)
from .grants import GrantResult, GrantedWorkResult, GuardOutcome, ProtectedAppend
from .mcp import closed_tool


def build_chatgpt_server(service: ChatGPTService, server: MCPServer | None = None) -> MCPServer:
    """A host may supply an OAuth-configured server; default stdio is test-adapter only."""
    server = server or MCPServer("Switchstand ChatGPT")

    async def grant_get(api_version: Literal["1"]) -> GrantResult:
        """Read this authenticated caller's current grant; this never issues or changes a grant."""
        return await service.grant_get()

    async def work_get(api_version: Literal["1"], work_id: UUID | None = None) -> GrantedWorkResult:
        """Read current granted work. Omit work_id for the server-selected active assignment."""
        return await service.get(work_id)

    async def source_task(api_version: Literal["1"], task_gid: str) -> SourceTaskResult:
        """Read current notes/state of one exact canonical source task; reading grants no work."""
        return await service.source_task(SourceTaskRequest(api_version=api_version, task_gid=task_gid))

    async def source_stories(
        api_version: Literal["1"], task_gid: str, observed_revision: str,
        offset: str | None = None, limit: int = 50,
    ) -> SourceStoriesResult:
        """Read a bounded current history page; stale/error is not an empty inbox."""
        return await service.source_stories(SourceStoriesRequest(
            api_version=api_version, task_gid=task_gid, observed_revision=observed_revision,
            offset=offset, limit=limit,
        ))

    async def source_story(
        api_version: Literal["1"], task_gid: str, story_gid: str, observed_revision: str,
    ) -> SourceStoryResult:
        """Reread an exact material story and its target/currentness before relying on it."""
        return await service.source_story(SourceStoryRequest(
            api_version=api_version, task_gid=task_gid, story_gid=story_gid,
            observed_revision=observed_revision,
        ))

    async def work_append(
        api_version: Literal["1"], operation_id: UUID, work_id: UUID,
        grant_version: int, observed_revision: str, text: str,
    ) -> GuardOutcome:
        """Append through the current grant. Reuse OperationId; UNKNOWN forbids new-ID retry."""
        return await service.append(ProtectedAppend(
            api_version=api_version, operation_id=operation_id, work_id=work_id,
            grant_version=grant_version, observed_revision=observed_revision, text=text,
        ))

    for name, function in (("grant_get", grant_get), ("work_get", work_get),
                           ("source_task", source_task), ("source_stories", source_stories),
                           ("source_story", source_story), ("work_append", work_append)):
        closed_tool(server, name, function)
    return server
