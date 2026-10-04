"""Private read-only current-snapshot evidence for one exact canonical WorkId."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from .canonical_relations import work_dependencies
from .canonical_work import canonical_work
from .work_events import work_events


def _time(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


async def _relations(
    connection: AsyncConnection, work_id: UUID,
) -> tuple[list[str], list[str], bool]:
    ancestry = (await connection.execute(text("""
        WITH RECURSIVE ancestry(work_id, depth, path, cycle) AS (
          SELECT parent_work_id, 1, ARRAY[child_work_id, parent_work_id], false
          FROM work_parents WHERE child_work_id = CAST(:work_id AS uuid)
          UNION ALL
          SELECT parent.parent_work_id, ancestry.depth + 1,
                 ancestry.path || parent.parent_work_id,
                 parent.parent_work_id = ANY(ancestry.path)
          FROM work_parents parent JOIN ancestry ON parent.child_work_id = ancestry.work_id
          WHERE NOT ancestry.cycle
        )
        SELECT work_id, cycle FROM ancestry ORDER BY depth
    """), {"work_id": str(work_id)})).all()
    dependencies = (await connection.scalars(select(
        work_dependencies.c.depends_on_work_id
    ).where(work_dependencies.c.work_id == work_id).order_by(
        work_dependencies.c.depends_on_work_id
    ))).all()
    return (
        [str(row[0]) for row in ancestry],
        [str(value) for value in dependencies],
        any(cast(bool, row[1]) for row in ancestry),
    )


async def _snapshot(connection: AsyncConnection, work_id: UUID) -> dict[str, object]:
    await connection.execute(text("SET TRANSACTION READ ONLY"))
    captured_at = cast(datetime, await connection.scalar(select(func.now())))
    isolation = cast(str, await connection.scalar(text("SHOW transaction_isolation")))
    read_only = cast(str, await connection.scalar(text("SHOW transaction_read_only")))
    work = (await connection.execute(select(
        canonical_work.c.row_version, canonical_work.c.admitted_at,
        canonical_work.c.canonical_root, canonical_work.c.completed,
        canonical_work.c.lifecycle_state, canonical_work.c.wait_kind,
        canonical_work.c.unblock_condition, canonical_work.c.next_due,
        canonical_work.c.next_action_class, canonical_work.c.next_action_ref,
    ).where(canonical_work.c.work_id == work_id))).one_or_none()
    if work is None:
        raise LookupError("canonical work does not exist")
    ancestors, dependencies, cycle = await _relations(connection, work_id)
    events = (await connection.execute(select(
        work_events.c.id, work_events.c.sequence, work_events.c.subtype,
        work_events.c.result_version, work_events.c.created_at,
    ).where(work_events.c.work_id == work_id).order_by(work_events.c.sequence))).all()

    raw_root = cast(str | None, work.canonical_root)
    root_id: UUID | None = None
    if raw_root not in {None, "NONE", "UNKNOWN"}:
        try:
            parsed = UUID(raw_root)
            root_id = parsed if str(parsed) == raw_root else None
        except ValueError:
            pass
    root_exists = root_id is not None and await connection.scalar(select(
        canonical_work.c.work_id
    ).where(canonical_work.c.work_id == root_id)) is not None
    if raw_root is None:
        root_status, root_reason = "UNKNOWN", "NOT_CAPTURED"
    elif raw_root == "NONE":
        root_status, root_reason = "NONE", None
    elif raw_root == "UNKNOWN":
        root_status, root_reason = "UNKNOWN", "EXPLICIT_UNKNOWN"
    elif root_id is None:
        root_status, root_reason = "UNKNOWN", "INVALID_WORK_ID"
    elif not root_exists:
        root_status, root_reason = "UNKNOWN", "DANGLING_WORK_ID"
    else:
        root_status, root_reason = "EXACT", None
    admission = cast(datetime | None, work.admitted_at)
    return {
        "schema": "switchstand.flow_report.v1", "status": "PARTIAL",
        "work_id": str(work_id), "captured_at": _time(captured_at),
        "snapshot": {"isolation": isolation, "read_only": read_only,
                     "row_version": work.row_version},
        "admission": {"status": "KNOWN" if admission else "UNKNOWN",
                      "at": _time(admission),
                      "reason": None if admission else "NOT_CAPTURED"},
        "current": {
            "completed": work.completed, "lifecycle_state": work.lifecycle_state,
            "wait_kind": work.wait_kind, "unblock_condition": work.unblock_condition,
            "next_due": work.next_due, "next_action_class": work.next_action_class,
            "next_action_ref": work.next_action_ref, "coverage": "CURRENT_ONLY",
        },
        "scope": {
            "canonical_root": {"status": root_status,
                               "work_id": str(root_id) if root_id is not None else None,
                               "reason": root_reason},
            "parent_ancestry": ancestors, "parent_cycle_detected": cycle,
            "dependencies": dependencies, "coverage": "CURRENT_ONLY",
        },
        "events": {
            "coverage": "OBSERVED_ONLY",
            "items": [{
                "id": str(row.id), "sequence": row.sequence,
                "subtype": row.subtype, "result_version": row.result_version,
                "at": _time(row.created_at),
            } for row in events],
        },
        "coverage": {
            "canonical_work": {"status": "INCLUDED", "reason": "CURRENT_ONLY"},
            "relations": {"status": "INCLUDED", "reason": "CURRENT_ONLY"},
            "work_events": {"status": "INCLUDED", "reason": "OBSERVED_ONLY"},
            "messages": {"status": "EXCLUDED", "reason": "AMBIGUOUS_ENDPOINT_NAMESPACE"},
            "timing_journal": {"status": "EXCLUDED", "reason": "RETENTION_NOT_PROVED"},
            **{name: {"status": "EXCLUDED", "reason": "NOT_INCLUDED_B1"} for name in (
                "human_trajectory", "outcome_state", "failures", "human_review",
                "lifecycle_timing", "github", "run_receipts",
            )},
        },
        "elapsed": "NOT_COMPUTED_B1",
    }


async def report(engine: AsyncEngine, work_id: UUID) -> dict[str, object]:
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="REPEATABLE READ")
        async with connection.begin():
            return await _snapshot(connection, work_id)


async def _run(work_id: UUID) -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        print(json.dumps(await report(engine, work_id), sort_keys=True, separators=(",", ":")))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Report exact read-only flow evidence")
    parser.add_argument("--work-id", type=UUID, required=True)
    arguments = parser.parse_args()
    asyncio.run(_run(arguments.work_id))
