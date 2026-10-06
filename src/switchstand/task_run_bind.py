"""Controller-side binding of one exact task-run request to a managed run."""

import argparse
import asyncio
import os
import sys
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .canonical_work import CanonicalWorkRepository
from .failure_journal import redact_environment
from .provision import require_current_schema
from .run import RunReceipt
from .task_runs import TaskRunBindResult, TaskRunState

MAX_RECEIPT_BYTES = 4096
OUTPUT_PREFIX = "TASK_RUN_BIND="


async def bind_task_start(
    engine: AsyncEngine, request_id: UUID, receipt: RunReceipt
) -> TaskRunBindResult:
    state = TaskRunState(engine, CanonicalWorkRepository(engine))
    return await state.bind_start(request_id, receipt)


async def _bind(request_id: UUID, receipt: RunReceipt) -> TaskRunBindResult:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        return await bind_task_start(engine, request_id, receipt)
    finally:
        await engine.dispose()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Bind one exact START request to a run receipt.")
    result.add_argument("--request-id", required=True, type=UUID)
    return result


def run(arguments: argparse.Namespace) -> None:
    payload = sys.stdin.buffer.read(MAX_RECEIPT_BYTES + 1)
    if not payload or len(payload) > MAX_RECEIPT_BYTES:
        raise ValueError("run receipt input is empty or exceeds 4096 bytes")
    receipt = RunReceipt.model_validate_json(payload)
    require_current_schema()
    result = asyncio.run(_bind(arguments.request_id, receipt))
    print(OUTPUT_PREFIX + result.model_dump_json())
    if result.status != "ok":
        raise SystemExit(2)


def main() -> None:
    arguments = parser().parse_args()
    try:
        run(arguments)
    except Exception as error:  # noqa: BLE001 - final secret-redacting CLI boundary
        detail = redact_environment(str(error), dict(os.environ))
        parser().exit(1, f"task-run bind failed: {detail[:4000]}\n")


if __name__ == "__main__":
    main()
