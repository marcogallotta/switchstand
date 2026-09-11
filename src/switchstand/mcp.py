import logging
import os
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

import httpx
from mcp.server import MCPServer
from sqlalchemy.ext.asyncio import create_async_engine

from .contracts import (
    AppendResult,
    LaunchAuthority,
    SuggestionResult,
    WorkAppendRequest,
    WorkGetRequest,
    WorkPatch,
    WorkResult,
    WorkUpdateRequest,
)
from .core import Controller
from .provider import AsanaProvider
from .state import PostgresState


def controller_from_env() -> Controller:
    references = tuple(UUID(value) for value in os.getenv("REFERENCE_WORK_IDS", "").split(",") if value)
    authority = LaunchAuthority(active_work_id=UUID(os.environ["ACTIVE_WORK_ID"]), reference_work_ids=references)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    client = httpx.AsyncClient(base_url="https://app.asana.com/api/1.0", trust_env=False,
                               headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"})
    return Controller(authority, PostgresState(engine), {"asana": AsanaProvider(client)})

def closed_tool(server: MCPServer, name: str, function: Callable[..., Any]) -> None:
    server.tool(name=name)(function)
    tool = server._tool_manager.get_tool(name)  # pyright: ignore[reportPrivateUsage]
    assert tool is not None
    tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
    tool.fn_metadata.arg_model.model_rebuild(force=True)
    tool.parameters = tool.fn_metadata.arg_model.model_json_schema(by_alias=True)

def build_server(
    service: object, active_work_id: UUID, reference_work_ids: tuple[UUID, ...] = ()
) -> MCPServer:
    server = MCPServer("Switchstand")

    async def _work_get(api_version: Literal["1"], work_id: UUID | None = None) -> WorkResult:
        return await service.get(WorkGetRequest(api_version=api_version, work_id=work_id or active_work_id))  # type: ignore[attr-defined]

    references = ", ".join(map(str, reference_work_ids)) or "none"
    _work_get.__doc__ = (
        "Read launch-bound work. Omit work_id for the active assignment. "
        f"Bounded read-only reference WorkIds: {references}."
    )

    async def _work_update(api_version: Literal["1"], work_id: UUID, observed_revision: str, patch: WorkPatch) -> WorkResult:
        """Update approved fields on the active work item."""
        return await service.update(WorkUpdateRequest(api_version=api_version, work_id=work_id, observed_revision=observed_revision, patch=patch))  # type: ignore[attr-defined]

    async def _work_append(api_version: Literal["1"], work_id: UUID, text: str) -> AppendResult:
        """Append one history entry to the active work item."""
        return await service.append(WorkAppendRequest(api_version=api_version, work_id=work_id, text=text))  # type: ignore[attr-defined]

    async def _work_suggest_next(api_version: Literal["1"]) -> SuggestionResult:
        """Return only the highest-priority unbound actionable work head."""
        return await service.suggest_next()  # type: ignore[attr-defined]

    closed_tool(server, "work_get", _work_get)
    closed_tool(server, "work_update", _work_update)
    closed_tool(server, "work_append", _work_append)
    closed_tool(server, "work_suggest_next", _work_suggest_next)
    return server


def server_from_env() -> MCPServer:
    if os.getenv("SWITCHSTAND_MANAGED") != "1":
        return MCPServer("Switchstand (unbound)")
    service = controller_from_env()
    return build_server(
        service, service.authority.active_work_id, service.authority.reference_work_ids
    )

def _protect_provider_logs() -> None:
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers[:] = [logging.NullHandler()]
        logger.propagate = False

def main() -> None:
    _protect_provider_logs()
    server_from_env().run()

if __name__ == "__main__":
    main()
