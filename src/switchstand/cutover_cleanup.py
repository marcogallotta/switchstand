"""Temporary exact-row cleanup for the frozen zero-Asana cutover."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from .grant_state import effect_intents
from .grants import CreateReceipt, EffectReceipt, GuardOutcome, RelationReceipt, UpdateReceipt
from .messages import message_projection
from .work_corpus import load_manifest, write_manifest


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
def _clean(value: object) -> object:
    return json.loads(json.dumps(value, default=str))
def _manifest(value: dict[str, object]) -> dict[str, object]:
    return value | {"sha256": _digest(value)}
async def _snapshot(
    connection: AsyncConnection, *, lock: bool,
) -> dict[str, list[dict[str, object]]]:
    effects_query = select(effect_intents).where(
        effect_intents.c.outcome["effect"].astext == "unknown"
    ).order_by(effect_intents.c.operation_id)
    projection_query = select(message_projection).order_by(message_projection.c.projection_id)
    if lock:
        effects_query, projection_query = effects_query.with_for_update(), projection_query.with_for_update()
    effects = [cast(dict[str, object], _clean(dict(row))) for row in (
        await connection.execute(effects_query)).mappings()]
    projections = [cast(dict[str, object], _clean(dict(row))) for row in (
        await connection.execute(projection_query)).mappings()]
    return {"effects": effects, "projections": projections}
async def cleanup_snapshot(engine: AsyncEngine) -> dict[str, object]:
    async with engine.connect() as connection:
        return await cleanup_snapshot_connection(connection)


async def cleanup_snapshot_connection(connection: AsyncConnection) -> dict[str, object]:
    """Snapshot cleanup rows using an already-owned database connection."""
    current = await _snapshot(connection, lock=False)
    return _manifest({
        "schema_version": 1,
        "effects": [{"row": row, "sha256": _digest(row)} for row in current["effects"]],
        "projections": [
            {"row": row, "sha256": _digest(row)} for row in current["projections"]
        ],
    })
def _actions(plan: dict[str, object], key: str) -> dict[str, dict[str, object]]:
    raw_value = plan.get(key)
    if (not isinstance(raw_value, list) or any(
            not isinstance(raw, dict) or not isinstance(cast(dict[object, object], raw).get("id"), str)
            for raw in cast(list[object], raw_value))):
        raise TypeError(f"cleanup plan {key} must be a list")
    actions = {cast(str, raw["id"]): cast(dict[str, object], raw) for raw in cast(list[dict[object, object]], raw_value)}
    if len(actions) != len(cast(list[object], raw_value)):
        raise ValueError(f"duplicate cleanup identity in {key}")
    return actions
def _receipt_matches(operation: object, receipt: object, row: dict[str, object]) -> bool:
    intent = cast(dict[str, object], row["intent"])
    request = cast(dict[str, object], intent["request"])
    if not all(getattr(receipt, key, None) == intent.get(key)
               for key in ("provider", "task_gid", "qualification")):
        return False
    if operation == "work_append":
        return isinstance(receipt, EffectReceipt) and receipt.text == request["text"]
    if operation == "work_create":
        return isinstance(receipt, CreateReceipt) and all((
            receipt.title == request["title"],
            receipt.parent_task_gid == intent.get("parent_task_gid"),
            receipt.project_gid == intent.get("project_gid"),
        ))
    if operation in {"work_update", "work_relate"}:
        kind = UpdateReceipt if operation == "work_update" else RelationReceipt
        return isinstance(receipt, kind) and all((
            receipt.observed_revision == request["observed_revision"],
            receipt.patch.model_dump(mode="json", exclude_none=True) == {
                key: value for key, value in cast(dict[str, object], request["patch"]).items()
                if value is not None
            },
        ))
    return False
async def apply_cleanup(engine: AsyncEngine, plan_path: Path, archive_path: Path) -> dict[str, int]:
    async with engine.begin() as connection:
        return await apply_cleanup_connection(connection, plan_path, archive_path)


async def apply_cleanup_connection(
    connection: AsyncConnection, plan_path: Path, archive_path: Path,
) -> dict[str, int]:
    """Apply reviewed cleanup inside the caller's already-open transaction."""
    plan = load_manifest(plan_path)
    if plan.get("schema_version") != 1:
        raise ValueError("cleanup plan schema is invalid")
    effect_actions, projection_actions = _actions(plan, "effects"), _actions(plan, "projections")
    replaced = deleted = projections_deleted = 0
    current = await _snapshot(connection, lock=True)
    effects = {cast(str, row["operation_id"]): row for row in current["effects"]}
    projections = {
        str(row["projection_id"]): row
        for row in current["projections"]
    }
    if set(effects) != set(effect_actions) or set(projections) != set(projection_actions):
        raise ValueError("cleanup plan does not name every current unresolved row")
    for identity, row in effects.items():
        action = effect_actions[identity]
        if action.get("sha256") != _digest(row):
            raise ValueError(f"effect row changed: {identity}")
        disposition = action.get("action")
        if disposition == "delete":
            await connection.execute(delete(effect_intents).where(
                effect_intents.c.operation_id == identity
            ))
            deleted += 1
        elif disposition == "replace":
            outcome = GuardOutcome.model_validate(action.get("outcome"))
            receipt = outcome.receipt
            operation = cast(dict[str, object], row["outcome"])["operation"]
            if (outcome.effect != "applied" or outcome.operation_id != UUID(identity)
                    or outcome.operation != operation
                    or outcome.work_id != UUID(cast(str, row["work_id"]))
                    or receipt is None or receipt.operation_id != outcome.operation_id
                    or receipt.work_id != outcome.work_id
                    or receipt.grant_id != UUID(cast(str, row["grant_id"]))
                    or receipt.grant_version != row["grant_version"]
                    or receipt.principal.key != row["principal_key"]
                    or not _receipt_matches(operation, receipt, row)):
                raise ValueError(f"replacement outcome identity is invalid: {identity}")
            await connection.execute(update(effect_intents).where(
                effect_intents.c.operation_id == identity
            ).values(outcome=outcome.model_dump(mode="json", exclude_none=True)))
            replaced += 1
        else:
            raise ValueError(f"unsupported effect disposition: {identity}")
    for identity, row in projections.items():
        action = projection_actions[identity]
        if action.get("action") != "delete" or action.get("sha256") != _digest(row):
            raise ValueError(f"projection row changed or disposition invalid: {identity}")
        await connection.execute(delete(message_projection).where(
            message_projection.c.projection_id == UUID(identity)
        ))
        projections_deleted += 1
    write_manifest(archive_path, _manifest({"schema_version": 1, **current}))
    directory = os.open(archive_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {"replaced": replaced, "deleted": deleted, "projections_deleted": projections_deleted}
async def _run(action: str, first: Path, second: Path | None) -> object:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        if action == "snapshot":
            document = await cleanup_snapshot(engine)
            write_manifest(first, document)
            return {"sha256": document["sha256"]}
        assert second is not None
        return await apply_cleanup(engine, first, second)
    finally:
        await engine.dispose()
def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("snapshot").add_argument("output", type=Path)
    apply = subparsers.add_parser("apply")
    apply.add_argument("plan", type=Path)
    apply.add_argument("archive", type=Path)
    arguments = parser.parse_args()
    result = asyncio.run(_run(arguments.action, arguments.output if arguments.action == "snapshot"
                              else arguments.plan,
                              None if arguments.action == "snapshot" else arguments.archive))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
