"""Offline Stage 3 reviewed-workset staging, reconciliation, and reset."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .stage12_cutover import read_private
from .work_index import ActivationUnknown, WorkIndex, canonical_digest
from .work_index_migration import require_offline
from .work_metadata import authority_generation
from .workset_worksheet import snapshot_from_reviewed_bytes
from .worksets import reconcile_staging, reset_staging, stage_snapshot


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
) -> str:
    """Run one exact pre-authority operation while every other DB client is stopped."""
    if not confirm_offline:
        raise ValueError("explicit --confirm-offline is required")
    data = read_private(worksheet_path, "Human-Reviewed Stage 3 worksheet")
    snapshot = snapshot_from_reviewed_bytes(data, expected_worksheet_digest)
    snapshot_digest = canonical_digest(snapshot.rows())
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    try:
        await require_offline(engine)
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
        raise ValueError("unsupported Stage 3 staging action")
    finally:
        await engine.dispose()


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "reconcile", "reset"))
    parser.add_argument("worksheet", type=Path)
    parser.add_argument("--expected-worksheet-digest", required=True)
    parser.add_argument("--confirm-offline", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        digest = asyncio.run(execute(
            arguments.action,
            arguments.worksheet,
            expected_worksheet_digest=arguments.expected_worksheet_digest,
            confirm_offline=arguments.confirm_offline,
        ))
    except ActivationUnknown as error:
        parser.exit(2, f"UNKNOWN: {error}\n")
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        parser.exit(1, f"Stage 3 {arguments.action} failed before authority flip: {error}\n")
    print(f"Stage 3 {arguments.action} complete snapshot_sha256={digest}")


if __name__ == "__main__":
    run()
