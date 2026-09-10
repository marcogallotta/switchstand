import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
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
from .run import read_receipt, update_receipt
from .state import PostgresState


async def controller_from_env() -> tuple[Controller, Path | None]:
    receipt_value = os.getenv("SWITCHSTAND_RUN_RECEIPT")
    receipt_path = Path(receipt_value) if receipt_value else None
    receipt = read_receipt(receipt_path) if receipt_path is not None else None
    if receipt is None:
        references = tuple(UUID(value) for value in os.getenv("REFERENCE_WORK_IDS", "").split(",") if value)
        authority = LaunchAuthority(active_work_id=UUID(os.environ["ACTIVE_WORK_ID"]), reference_work_ids=references)
    else:
        assert receipt_path is not None
        prepared = receipt.prepared
        if prepared is None:
            update_receipt(receipt_path, phase="BLOCKED", outcome="intake incomplete")
            raise RuntimeError("launch blocked: rerun exact-task intake")
        if os.environ.get("SWITCHSTAND_CANDIDATE_SHA") != receipt.candidate_sha:
            update_receipt(receipt_path, phase="BLOCKED", outcome="candidate identity mismatch")
            raise RuntimeError("launch blocked: rebuild the exact candidate")
        authority = prepared.authority
    engine = create_async_engine(os.environ["DATABASE_URL"])
    client = httpx.AsyncClient(base_url="https://app.asana.com/api/1.0", trust_env=False,
                               headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"})
    controller = Controller(authority, PostgresState(engine), {"asana": AsanaProvider(client)},
                            admitted=receipt_path is None)
    if receipt_path is not None and receipt is not None:
        update_receipt(receipt_path, phase="VERIFYING", controller_identity=str(os.getpid()))
        assert receipt.prepared is not None
        if not await controller.verify_and_admit(receipt.prepared.verification):
            update_receipt(receipt_path, phase="BLOCKED", outcome="work verification mismatch")
            raise RuntimeError("launch blocked: rerun exact-task intake")
        update_receipt(receipt_path, phase="ADMITTED", admitted=True)
    return controller, receipt_path

def closed_tool(server: MCPServer, name: str, function: Callable[..., Any]) -> None:
    server.tool(name=name)(function)
    tool = server._tool_manager.get_tool(name)  # pyright: ignore[reportPrivateUsage]
    assert tool is not None
    tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
    tool.fn_metadata.arg_model.model_rebuild(force=True)
    tool.parameters = tool.fn_metadata.arg_model.model_json_schema(by_alias=True)

def build_server(
    service: object, active_work_id: UUID, reference_work_ids: tuple[UUID, ...] = (),
    on_active_read: Callable[[], None] | None = None,
) -> MCPServer:
    server = MCPServer("Switchstand")

    async def _work_get(api_version: Literal["1"], work_id: UUID | None = None) -> WorkResult:
        target = work_id or active_work_id
        result = cast(
            WorkResult,
            await service.get(  # type: ignore[attr-defined]
                WorkGetRequest(api_version=api_version, work_id=target)
            ),
        )
        if target == active_work_id and result.item is not None:
            result = result.model_copy(update={"launch": LaunchBinding(
                active_work_id=active_work_id,
                reference_work_ids=reference_work_ids,
                reference_count=len(reference_work_ids),
            )})
            if on_active_read is not None:
                on_active_read()
        return result

    _work_get.__doc__ = (
        "Read launch-bound work. Omit work_id for the active assignment. "
        "The active result includes the authoritative bounded reference WorkIds and count."
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

def _protect_provider_logs() -> None:
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers[:] = [logging.NullHandler()]
        logger.propagate = False

async def _serve() -> None:
    _protect_provider_logs()
    service, receipt_path = await controller_from_env()
    if receipt_path is not None:
        update_receipt(receipt_path, phase="STARTING")
    running = False

    def on_active_read() -> None:
        nonlocal running
        if receipt_path is not None and not running:
            update_receipt(receipt_path, phase="RUNNING")
            running = True

    await build_server(
        service, service.authority.active_work_id, service.authority.reference_work_ids,
        on_active_read,
    ).run_stdio_async()


def main() -> None:
    asyncio.run(_serve())

if __name__ == "__main__":
    main()
