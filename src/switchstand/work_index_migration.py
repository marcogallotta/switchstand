"""One-shot offline Stage 1 import and irreversible authority flip."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .contracts import Routing, WorkContext
from .discovery import ProviderSearchItem
from .state import work_handles
from .work_corpus import load_manifest, manifest_exception_digest
from .work_index import (
    ActivationReceipt,
    ActivationUnknown,
    activate,
    prepare_manifest,
    reconcile_activation,
)


@dataclass(frozen=True)
class FrozenCorpus:
    items: tuple[ProviderSearchItem, ...]
    work_ids: dict[str, UUID | None]
    exceptions: dict[str, UUID]
    manifest_digest: str
    exception_digest: str


def _stable_corpus(
    paths: tuple[Path, Path],
    expected_exception_digest: str,
) -> FrozenCorpus:
    first, second = (load_manifest(path) for path in paths)
    if first != second:
        raise ValueError("the two complete corpus manifests do not match")
    if first.get("schema_version") != 1 or not isinstance(first.get("rows"), list):
        raise ValueError("unsupported corpus manifest")
    source_candidate = first.get("source_candidate")
    if (
        not isinstance(source_candidate, str)
        or len(source_candidate) != 40
        or any(character not in "0123456789abcdef" for character in source_candidate)
    ):
        raise ValueError("invalid corpus source candidate")
    raw_exceptions = first.get("exceptions")
    if not isinstance(raw_exceptions, list):
        raise TypeError("invalid corpus exceptions")
    exception_digest = manifest_exception_digest(first)
    if exception_digest != expected_exception_digest:
        raise ValueError("corpus exception digest is not the reviewed digest")

    items: list[ProviderSearchItem] = []
    work_ids: dict[str, UUID | None] = {}
    dependencies: dict[str, set[str]] = {}
    for raw in cast(list[object], first["rows"]):
        if not isinstance(raw, dict):
            raise TypeError("invalid corpus row")
        row = cast(dict[str, object], raw)
        if set(row) != {
            "provider_work_id",
            "work_id",
            "title",
            "completed",
            "revision",
            "routing",
            "context",
            "dependencies",
        }:
            raise ValueError("invalid corpus row")
        provider_id = row["provider_work_id"]
        raw_dependencies = row["dependencies"]
        title, completed, revision = row["title"], row["completed"], row["revision"]
        raw_work_id = row["work_id"]
        if (
            not isinstance(provider_id, str)
            or not provider_id
            or provider_id in work_ids
            or not isinstance(raw_dependencies, list)
            or not isinstance(title, str)
            or not isinstance(completed, bool)
            or not isinstance(revision, str)
            or not revision
            or (raw_work_id is not None and not isinstance(raw_work_id, str))
            or any(
                not isinstance(value, str) or not value
                for value in cast(list[object], raw_dependencies)
            )
        ):
            raise ValueError("invalid corpus identity or dependencies")
        work_id = None if raw_work_id is None else UUID(raw_work_id)
        work_ids[provider_id] = work_id
        dependencies[provider_id] = set(cast(list[str], raw_dependencies))
        items.append(
            ProviderSearchItem(
                provider_work_id=provider_id,
                title=title,
                completed=completed,
                revision=revision,
                routing=Routing.model_validate(row["routing"]),
                context=WorkContext.model_validate(row["context"]),
            )
        )
    if not items or any(not values <= set(work_ids) for values in dependencies.values()):
        raise ValueError("corpus dependencies must remain inside the included corpus")

    exceptions: dict[str, UUID] = {}
    for raw in cast(list[object], raw_exceptions):
        if not isinstance(raw, dict):
            raise TypeError("invalid corpus exception")
        row = cast(dict[str, object], raw)
        if set(row) != {"provider_work_id", "work_id", "reason"}:
            raise ValueError("invalid corpus exception")
        provider_id = row["provider_work_id"]
        exception_work_id = row["work_id"]
        if (
            not isinstance(provider_id, str)
            or not isinstance(exception_work_id, str)
            or provider_id in work_ids
            or provider_id in exceptions
            or row["reason"] not in {"missing", "noncanonical"}
        ):
            raise ValueError("invalid corpus exception identity")
        exceptions[provider_id] = UUID(exception_work_id)
    counts = first.get("counts")
    if not isinstance(counts, dict):
        raise TypeError("corpus manifest counts do not match its contents")
    count_values = cast(dict[str, object], counts)
    if (
        set(count_values) != {"broad", "bound", "included", "exceptions"}
        or any(type(value) is not int or value < 0 for value in count_values.values())
        or count_values["included"] != len(items)
        or count_values["exceptions"] != len(exceptions)
        or count_values["bound"]
        != sum(value is not None for value in work_ids.values()) + len(exceptions)
        or cast(int, count_values["broad"]) > len(items)
    ):
        raise ValueError("corpus manifest counts do not match its contents")
    return FrozenCorpus(
        tuple(items),
        work_ids,
        exceptions,
        cast(str, first["sha256"]),
        exception_digest,
    )


async def _validate_bindings(
    engine: AsyncEngine,
    corpus: FrozenCorpus,
    *,
    prepared: bool,
) -> None:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(
                    work_handles.c.provider_work_id,
                    work_handles.c.id,
                ).where(work_handles.c.provider == "asana")
            )
        ).all()
    actual = {provider_id: work_id for provider_id, work_id in rows}
    expected_ids = (
        set(corpus.work_ids)
        if prepared
        else {
            provider_id for provider_id, work_id in corpus.work_ids.items() if work_id is not None
        }
    ) | set(corpus.exceptions)
    if set(actual) != expected_ids:
        raise ValueError("Asana bindings changed after corpus capture")
    for provider_id, expected in corpus.work_ids.items():
        if expected is not None and actual.get(provider_id) != expected:
            raise ValueError("included corpus binding identity changed")
    if any(
        actual.get(provider_id) != work_id for provider_id, work_id in corpus.exceptions.items()
    ):
        raise ValueError("exception binding identity changed")


async def require_offline(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        others = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND backend_type = 'client backend'"
            )
        )
    if others:
        raise RuntimeError("Stage 1 requires the MCP service and every other DB client stopped")


def write_receipt(path: Path, receipt: ActivationReceipt) -> None:
    """Durably publish the exact attempted post/pre state before COMMIT starts."""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(asdict(receipt), stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def load_receipt(path: Path) -> ActivationReceipt:
    return ActivationReceipt(**json.loads(path.read_text(encoding="utf-8")))


async def migrate(
    action: str,
    *,
    confirm_offline: bool,
    expected_manifest_digest: str | None,
    receipt_path: Path | None,
    manifest_paths: tuple[Path, Path] | None,
    expected_exception_digest: str | None,
) -> str | ActivationReceipt:
    if not confirm_offline:
        raise ValueError("explicit --confirm-offline is required")
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    if action == "reconcile":
        try:
            if receipt_path is None:
                raise ValueError("--receipt is required")
            return await reconcile_activation(engine, load_receipt(receipt_path))
        finally:
            await engine.dispose()
    try:
        await require_offline(engine)
        if manifest_paths is None or expected_exception_digest is None:
            raise ValueError("prepare/activate require two manifests and an exception digest")
        corpus = _stable_corpus(manifest_paths, expected_exception_digest)
        await _validate_bindings(engine, corpus, prepared=action == "activate")
        await require_offline(engine)
        if action == "prepare":
            return await prepare_manifest(engine, corpus.items)
        if expected_manifest_digest is None or receipt_path is None:
            raise ValueError("activate requires --expected-manifest-digest and --receipt")
        return await activate(
            engine,
            corpus.items,
            expected_manifest_digest=expected_manifest_digest,
            before_commit=lambda receipt: write_receipt(receipt_path, receipt),
        )
    finally:
        await engine.dispose()


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "activate", "reconcile"))
    parser.add_argument("--confirm-offline", action="store_true")
    parser.add_argument("--expected-manifest-digest")
    parser.add_argument("--manifest", action="append", type=Path, default=[])
    parser.add_argument("--expected-exception-digest")
    parser.add_argument("--receipt", type=Path)
    arguments = parser.parse_args(argv)
    try:
        receipt = asyncio.run(
            migrate(
                arguments.action,
                confirm_offline=arguments.confirm_offline,
                expected_manifest_digest=arguments.expected_manifest_digest,
                receipt_path=arguments.receipt,
                manifest_paths=tuple(arguments.manifest) if len(arguments.manifest) == 2 else None,
                expected_exception_digest=arguments.expected_exception_digest,
            )
        )
    except ActivationUnknown as error:
        parser.exit(2, f"{error}\n")
    except (KeyError, OSError, RuntimeError, SQLAlchemyError, TypeError, ValueError) as error:
        parser.exit(1, f"Stage 1 migration failed before authority flip: {error}\n")
    if isinstance(receipt, str):
        print(f"prepared_manifest_sha256={receipt}")
        return
    print(
        f"POSTGRES_AUTHORITY active generation={receipt.generation} count={receipt.count} "
        f"corpus_sha256={receipt.corpus_digest} "
        f"recovered_after_commit_error={str(receipt.recovered_after_commit_error).lower()}"
    )


if __name__ == "__main__":
    run()
