"""Offline Stage 3 reviewed-workset staging, reconciliation, and reset."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .stage12_cutover import read_private
from .work_index import (
    ActivationNotCommitted,
    ActivationReceipt,
    ActivationUnknown,
    WorkIndex,
    canonical_digest,
)
from .work_index_migration import load_receipt, require_offline, write_receipt
from .work_metadata import authority_generation
from .workset_worksheet import snapshot_from_reviewed_bytes
from .worksets import (
    activate,
    reconcile_activation,
    reconcile_staging,
    reset_staging,
    stage_snapshot,
)


async def _require_prior_authority(engine: AsyncEngine) -> None:
    if await WorkIndex(engine).generation() is None:
        raise ValueError("Stage 3 staging requires Stage 1 authority")
    if await authority_generation(engine) is None:
        raise ValueError("Stage 3 staging requires Stage 2 authority")


async def execute(
    action: str,
    worksheet_path: Path,
    *,
    expected_worksheet_digest: str,
    confirm_offline: bool,
    receipt_path: Path | None = None,
) -> str | ActivationReceipt:
    """Run one exact pre-authority operation while every other DB client is stopped."""
    if not confirm_offline:
        raise ValueError("explicit --confirm-offline is required")
    data = read_private(worksheet_path, "Human-Reviewed Stage 3 worksheet")
    snapshot = snapshot_from_reviewed_bytes(data, expected_worksheet_digest)
    snapshot_digest = canonical_digest(snapshot.rows())
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    try:
        await require_offline(engine)
        if action == "reconcile-activation":
            if receipt_path is None:
                raise ValueError("--receipt is required")
            receipt = load_receipt(receipt_path)
            if receipt.corpus_digest != snapshot_digest:
                raise ValueError("activation receipt does not match the reviewed worksheet")
            return await reconcile_activation(engine, receipt)
        await _require_prior_authority(engine)
        if action == "stage":
            if await stage_snapshot(engine, snapshot) != snapshot_digest:
                raise RuntimeError("Stage 3 staging returned an unexpected digest")
            return await reconcile_staging(engine, snapshot_digest)
        if action == "reconcile":
            return await reconcile_staging(engine, snapshot_digest)
        if action == "reset":
            await reset_staging(engine, snapshot_digest)
            return snapshot_digest
        if action == "activate":
            if receipt_path is None:
                raise ValueError("--receipt is required")
            return await activate(
                engine, snapshot_digest,
                before_commit=lambda receipt: write_receipt(receipt_path, receipt),
            )
        raise ValueError("unsupported Stage 3 staging action")
    finally:
        await engine.dispose()


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("stage", "reconcile", "reset", "activate", "reconcile-activation")
    )
    parser.add_argument("worksheet", type=Path)
    parser.add_argument("--expected-worksheet-digest", required=True)
    parser.add_argument("--confirm-offline", action="store_true")
    parser.add_argument("--receipt", type=Path)
    arguments = parser.parse_args(argv)
    try:
        digest = asyncio.run(execute(
            arguments.action,
            arguments.worksheet,
            expected_worksheet_digest=arguments.expected_worksheet_digest,
            confirm_offline=arguments.confirm_offline,
            receipt_path=arguments.receipt,
        ))
    except ActivationNotCommitted as error:
        parser.exit(3, f"NOT_COMMITTED: {error}\n")
    except ActivationUnknown as error:
        parser.exit(2, f"UNKNOWN: {error}\n")
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        parser.exit(1, f"Stage 3 {arguments.action} failed before authority flip: {error}\n")
    if isinstance(digest, ActivationReceipt):
        print(
            f"Stage 3 POSTGRES_AUTHORITY active generation={digest.generation} "
            f"count={digest.count} snapshot_sha256={digest.corpus_digest} "
            f"recovered_after_commit_error="
            f"{str(digest.recovered_after_commit_error).lower()}"
        )
    else:
        print(f"Stage 3 {arguments.action} complete snapshot_sha256={digest}")


if __name__ == "__main__":
    run()
