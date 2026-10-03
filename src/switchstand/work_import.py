"""Temporary one-shot canonical import and target parity export."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy import DateTime, Table, delete, func, insert, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from .bootstrap_identity import MIGRATION_COMPLETE_RECEIPT
from .canonical_relations import (
    project_memberships,
    projects,
    work_dependencies,
    work_parents,
)
from .canonical_work import canonical_work, legacy_work_aliases
from .state import work_event_handles, work_handles, work_migration_receipts
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


def _retired_bindings(path: Path) -> set[tuple[str, UUID]]:
    document = load_manifest(path)
    exceptions = document.get("exceptions")
    if not isinstance(exceptions, list):
        raise TypeError("source corpus exceptions are invalid")
    result: set[tuple[str, UUID]] = set()
    for raw in cast(list[object], exceptions):
        if not isinstance(raw, dict):
            raise TypeError("source corpus exception must be an object")
        item = cast(dict[str, object], raw)
        if set(item) != {"provider_work_id", "work_id", "reason"}:
            raise ValueError("source corpus exception identity is invalid")
        if item["reason"] != "zero-membership":
            continue
        gid, work_id = item["provider_work_id"], item["work_id"]
        if not isinstance(gid, str) or not gid or not isinstance(work_id, str):
            raise ValueError("source corpus exception identity is invalid")
        binding = gid, UUID(work_id)
        if binding in result:
            raise ValueError("source corpus contains duplicate retired identity")
        result.add(binding)
    return result


async def _retire_legacy_event_handles(
    connection: AsyncConnection, grouped: dict[str, list[dict[str, object]]],
    corpus_path: Path | None,
) -> None:
    source_aliases = {
        (
            cast(str, cast(dict[str, object], record["fields"])["asana_task_gid"]),
            UUID(cast(str, cast(dict[str, object], record["fields"])["work_id"])),
        )
        for record in grouped["alias"]
    }
    legacy_bindings = {
        (gid, work_id)
        for gid, work_id in (await connection.execute(select(
            work_handles.c.provider_work_id, work_handles.c.id,
        ).where(work_handles.c.provider == "asana"))).all()
    }
    omitted = legacy_bindings - source_aliases
    if corpus_path is None:
        if not omitted:
            return
        raise ValueError("source omits Asana identities but no source corpus was provided")
    retired = _retired_bindings(corpus_path)
    if retired != omitted:
        raise ValueError("source corpus retired identities do not match omitted Asana bindings")
    if not retired:
        return
    retired_work_ids = {work_id for _, work_id in retired}
    await connection.execute(delete(work_event_handles).where(
        work_event_handles.c.work_id.in_(retired_work_ids)
    ))


async def import_parity(
    engine: AsyncEngine, source: Path, corpus_path: Path | None = None,
) -> int:
    """Fail atomically unless every canonical target table is empty and valid."""
    source_document = load_manifest(source)
    grouped = _records(source_document)
    imported = 0
    async with engine.begin() as connection:
        for _, table, _ in TABLES:
            if await connection.scalar(select(func.count()).select_from(table)):
                raise ValueError(f"import target table is not empty: {table.name}")
        await _retire_legacy_event_handles(connection, grouped, corpus_path)
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
        records: list[dict[str, object]] = []
        for kind, table, keys in TABLES:
            read_rows = (await connection.execute(select(table).order_by(
                *[table.c[key] for key in keys]
            ))).mappings()
            for row in read_rows:
                fields = {key: parity_value(value) for key, value in row.items()}
                records.append({"kind": kind, "id": _identity(fields, keys), "fields": fields})
        source_records = {
            (kind, cast(str, record["id"])): record["fields"]
            for kind, _, _ in TABLES for record in grouped[kind]
        }
        readback_records = {
            (cast(str, record["kind"]), cast(str, record["id"])): record["fields"]
            for record in records
        }
        if readback_records != source_records:
            raise ValueError("target readback does not match source parity")
        await connection.execute(insert(work_migration_receipts).values(
            name=MIGRATION_COMPLETE_RECEIPT,
            source_digest=hashlib.sha256(source.read_bytes()).hexdigest(),
        ))
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


async def _run(action: str, path: Path, corpus_path: Path | None = None) -> str:
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    try:
        if action == "import":
            return f"imported_records={await import_parity(engine, path, corpus_path)}"
        manifest = await target_parity(engine)
        write_manifest(path, manifest)
        return f"target_records={len(cast(Sequence[object], manifest['records']))}"
    finally:
        await engine.dispose()


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("import", "export"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--corpus", type=Path)
    arguments = parser.parse_args(argv)
    try:
        print(asyncio.run(_run(arguments.action, arguments.path, arguments.corpus)))
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.exit(1, f"Work import failed: {error}\n")


if __name__ == "__main__":
    run()
