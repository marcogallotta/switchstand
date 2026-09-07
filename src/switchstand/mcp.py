from typing import Literal
from uuid import UUID

from mcp.server import MCPServer

from .contracts import (
    AppendResult,
    WorkAppendRequest,
    WorkGetRequest,
    WorkPatch,
    WorkResult,
    WorkUpdateRequest,
)


class UnavailableController:
    async def get(self, request: WorkGetRequest) -> WorkResult:
        return WorkResult(status="provider_error")
    async def update(self, request: WorkUpdateRequest) -> WorkResult:
        return WorkResult(status="provider_error")
    async def append(self, request: WorkAppendRequest) -> AppendResult:
        return AppendResult(status="provider_error")

def build_server(controller: object | None = None) -> MCPServer:
    service = controller or UnavailableController()
    server = MCPServer("Switchstand")

    async def _work_get(api_version: Literal["1"], work_id: UUID) -> WorkResult:
        """Read one launch-bound work item."""
        return await service.get(WorkGetRequest(api_version=api_version, work_id=work_id))  # type: ignore[attr-defined]

    async def _work_update(api_version: Literal["1"], work_id: UUID, observed_revision: str, patch: WorkPatch) -> WorkResult:
        """Update approved fields on the active work item."""
        return await service.update(WorkUpdateRequest(api_version=api_version, work_id=work_id, observed_revision=observed_revision, patch=patch))  # type: ignore[attr-defined]

    async def _work_append(api_version: Literal["1"], work_id: UUID, text: str) -> AppendResult:
        """Append one history entry to the active work item."""
        return await service.append(WorkAppendRequest(api_version=api_version, work_id=work_id, text=text))  # type: ignore[attr-defined]
    server.tool(name="work_get")(_work_get)
    server.tool(name="work_update")(_work_update)
    server.tool(name="work_append")(_work_append)
    return server

def main() -> None:
    build_server().run()

if __name__ == "__main__":
    main()
