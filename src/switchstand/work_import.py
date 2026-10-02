"""Temporary one-shot canonical import and target parity export."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy import DateTime, Table, func, insert, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .canonical_relations import (
    project_memberships,
    projects,
    work_dependencies,
    work_parents,
)
from .canonical_work import canonical_work, legacy_work_aliases
from .work_corpus import load_manifest, parity_manifest, parity_value, write_manifest
from .work_events import work_events

TableSpec = tuple[str, Table, tuple[str, ...]]
TABLES: tuple[TableSpec, ...] = (
    ("work", canonical_work, ("work_id",)),
    ("alias", legacy_work_aliases, ("asana_task_gid",)),
    ("project", projects, ("project_id",)),
    ("parent", work_parents, ("child_work_id",)),
    ("dependency", work_dependencies, ("work_id", "depends_on_work_id")),
    ("membership", project_memberships, ("project_id", "work_id")),
    ("event", work_events, ("id",)),
)


def _identity(fields: dict[str, object], keys: tuple[str, ...]) -> str:
    return json.dumps([fields[key] for key in keys], separators=(",", ":"))


def _values(table: Table, raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise TypeError("import fields must be an object")
    fields = cast(dict[object, object], raw)
    expected = {column.name for column in table.columns}
    if set(fields) != expected or not all(isinstance(key, str) for key in fields):
        raise ValueError(f"{table.name} import fields do not match its columns")
    values: dict[str, object] = {}
    for column in table.columns:
        value = fields[column.name]
        if value is not None and isinstance(column.type, PGUUID):
            value = UUID(cast(str, value))
        elif value is not None and isinstance(column.type, DateTime):
            value = datetime.fromisoformat(cast(str, value))
        values[column.name] = value
    return values


def _records(document: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    records = document.get("records")
    if not isinstance(records, list):
        raise TypeError("import is not a canonical parity manifest")
    raw_records = cast(list[object], records)
    if parity_manifest(raw_records) != document:
        raise ValueError("import is not a canonical parity manifest")
    grouped: dict[str, list[dict[str, object]]] = {
        kind: [] for kind, _, _ in TABLES
    }
    for raw in raw_records:
        record = cast(dict[str, object], raw)
        kind = cast(str, record["kind"])
        if kind not in grouped:
            raise ValueError(f"unsupported import record kind: {kind}")
        grouped[kind].append(record)
    if not grouped["work"]:
        raise ValueError("import contains no canonical work")
    return grouped


async def import_parity(engine: AsyncEngine, source: Path) -> int:
    """Fail atomically unless every canonical target table is empty and valid."""
    grouped = _records(load_manifest(source))
    imported = 0
    async with engine.begin() as connection:
        for _, table, _ in TABLES:
            if await connection.scalar(select(func.count()).select_from(table)):
                raise ValueError(f"import target table is not empty: {table.name}")
        for kind, table, keys in TABLES:
            rows: list[dict[str, object]] = []
            for record in grouped[kind]:
                values = _values(table, record["fields"])
                exported = {key: parity_value(value) for key, value in values.items()}
                if record["id"] != _identity(exported, keys):
                    raise ValueError(f"{kind} import identity does not match its fields")
                rows.append(values)
            if rows:
                await connection.execute(insert(table), rows)
                imported += len(rows)
    return imported


async def target_parity(engine: AsyncEngine) -> dict[str, object]:
    """Export every canonical target row and field for exact source comparison."""
    records: list[dict[str, object]] = []
    async with engine.connect() as connection:
        for kind, table, keys in TABLES:
            rows = (await connection.execute(select(table).order_by(
                *[table.c[key] for key in keys]
            ))).mappings()
            for row in rows:
                fields = {key: parity_value(value) for key, value in row.items()}
                records.append({"kind": kind, "id": _identity(fields, keys), "fields": fields})
    return parity_manifest(records)


async def _run(action: str, path: Path) -> str:
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    try:
        if action == "import":
            return f"imported_records={await import_parity(engine, path)}"
        manifest = await target_parity(engine)
        write_manifest(path, manifest)
        return f"target_records={len(cast(Sequence[object], manifest['records']))}"
    finally:
        await engine.dispose()


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("import", "export"))
    parser.add_argument("path", type=Path)
    arguments = parser.parse_args(argv)
    try:
        print(asyncio.run(_run(arguments.action, arguments.path)))
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.exit(1, f"Work import failed: {error}\n")


if __name__ == "__main__":
    run()
