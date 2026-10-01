"""One-time offline Stage 2 worksheet generation, validation, and authority flip."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import httpx
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .core import ProviderError
from .provider import AsanaProvider
from .work_index import ActivationReceipt, ActivationUnknown
from .work_index_migration import load_receipt, require_offline, write_receipt
from .work_metadata import (
    ImportWorksheet,
    ProviderMetadataSnapshot,
    WorksheetRow,
    activate_metadata,
    generate_worksheet,
    reconcile_metadata_activation,
    validate_worksheet,
)


async def provider_snapshots(
    provider: AsanaProvider, worksheet: ImportWorksheet,
) -> tuple[ProviderMetadataSnapshot, ...]:
    snapshots: list[ProviderMetadataSnapshot] = []
    for row in worksheet.rows:
        work = await provider.get(row.provider_work_id)
        if work is None or not work.canonical:
            raise ValueError(f"provider work unavailable: {row.provider_work_id}")
        dependencies = await provider.dependencies_for_import(row.provider_work_id)
        revalidated = await provider.get(row.provider_work_id)
        if (
            revalidated is None or not revalidated.canonical
            or revalidated.revision != work.revision
            or revalidated.routing != work.routing
        ):
            raise ValueError(f"provider work changed during snapshot: {row.provider_work_id}")
        snapshots.append(ProviderMetadataSnapshot(
            provider_work_id=row.provider_work_id,
            revision=work.revision,
            routing=work.routing,
            notes=work.notes,
            context=work.context,
            dependency_provider_ids=dependencies,
        ))
    return tuple(snapshots)


async def populate_dependencies(
    worksheet: ImportWorksheet,
    snapshots: tuple[ProviderMetadataSnapshot, ...],
) -> ImportWorksheet:
    provider_to_work = {row.provider_work_id: row.work_id for row in worksheet.rows}
    by_provider = {snapshot.provider_work_id: snapshot for snapshot in snapshots}
    rows: list[WorksheetRow] = []
    for row in worksheet.rows:
        snapshot = by_provider[row.provider_work_id]
        try:
            dependencies = tuple(sorted(
                (provider_to_work[value] for value in snapshot.dependency_provider_ids),
                key=lambda value: value.int,
            ))
        except KeyError as error:
            raise ValueError("dependency points outside the admitted Stage 1 corpus") from error
        rows.append(row.model_copy(update={
            "provider_revision": snapshot.revision,
            "priority": snapshot.routing.priority,
            "work_type": snapshot.routing.work_type,
            "review_next_action": snapshot.routing.review_next_action,
            "horizon": snapshot.routing.horizon,
            "stage3_gate": snapshot.routing.stage3_gate,
            "depends_on": dependencies,
        }))
    return worksheet.model_copy(update={"rows": tuple(rows)})


def load_worksheet(path: Path) -> ImportWorksheet:
    return ImportWorksheet.model_validate_json(path.read_text(encoding="utf-8"))


async def _resources() -> tuple[AsyncEngine, httpx.AsyncClient, AsanaProvider]:
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", trust_env=False,
        headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"},
    )
    return engine, client, AsanaProvider(client, os.getenv("SWITCHSTAND_TEST_PROJECT_GID"))


async def execute(
    action: str, path: Path, *, confirm_offline: bool, receipt_path: Path | None,
) -> int | ActivationReceipt:
    if not confirm_offline:
        raise ValueError("explicit --confirm-offline is required")
    if action == "reconcile":
        if receipt_path is None:
            raise ValueError("--receipt is required")
        engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
        try:
            await require_offline(engine)
            return await reconcile_metadata_activation(engine, load_receipt(receipt_path))
        finally:
            await engine.dispose()
    engine, client, provider = await _resources()
    try:
        await require_offline(engine)
        if action == "generate":
            worksheet = await generate_worksheet(engine)
            worksheet = await populate_dependencies(
                worksheet, await provider_snapshots(provider, worksheet)
            )
            path.write_text(worksheet.model_dump_json(indent=2) + "\n", encoding="utf-8")
            return len(worksheet.rows)
        worksheet = load_worksheet(path)
        snapshots = await provider_snapshots(provider, worksheet)
        await require_offline(engine)
        if action == "validate":
            await validate_metadata(engine, worksheet, snapshots)
            return len(worksheet.rows)
        if receipt_path is None:
            raise ValueError("activate requires --receipt")
        return await activate_metadata(
            engine, worksheet, snapshots,
            before_commit=lambda receipt: write_receipt(receipt_path, receipt),
        )
    finally:
        await client.aclose()
        await engine.dispose()


async def validate_metadata(
    engine: AsyncEngine,
    worksheet: ImportWorksheet,
    snapshots: tuple[ProviderMetadataSnapshot, ...],
) -> None:
    """Validate the complete frozen corpus without writing or flipping authority."""
    await validate_worksheet(engine, worksheet, snapshots)


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "validate", "activate", "reconcile"))
    parser.add_argument("worksheet", type=Path)
    parser.add_argument("--confirm-offline", action="store_true")
    parser.add_argument("--receipt", type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(execute(
            arguments.action, arguments.worksheet, confirm_offline=arguments.confirm_offline
            , receipt_path=arguments.receipt
        ))
    except ActivationUnknown as error:
        parser.exit(2, f"{error}\n")
    except (KeyError, OSError, ProviderError, RuntimeError, SQLAlchemyError, ValidationError, ValueError) as error:
        parser.exit(1, f"Stage 2 {arguments.action} failed before authority flip: {error}\n")
    if isinstance(result, ActivationReceipt):
        print(
            f"Stage 2 POSTGRES_AUTHORITY active generation={result.generation} "
            f"count={result.count} corpus_sha256={result.corpus_digest} "
            f"edges_sha256={result.edge_digest} "
            f"recovered_after_commit_error={str(result.recovered_after_commit_error).lower()}"
        )
    else:
        print(f"Stage 2 {arguments.action} complete for {result} admitted work items")


if __name__ == "__main__":
    run()
