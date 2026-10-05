import argparse
import asyncio
import json
import os
from uuid import UUID

from sqlalchemy.ext.asyncio import create_async_engine

from .agent_mailboxes import AgentMailboxState


async def _approve(request_id: UUID) -> dict[str, object]:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    try:
        result = await AgentMailboxState(engine).approve_transfer(request_id)
        return result.model_dump(mode="json", exclude_none=True)
    finally:
        await engine.dispose()


def run() -> None:
    parser = argparse.ArgumentParser(description="Approve one exact agent-name transfer request.")
    parser.add_argument("request_id", type=UUID)
    request_id = parser.parse_args().request_id
    try:
        result = asyncio.run(_approve(request_id))
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    if result["status"] != "approved":
        raise SystemExit(2)
