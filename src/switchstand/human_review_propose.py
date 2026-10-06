"""Host-only preparation of one exact Human Review consequence."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from sqlalchemy.ext.asyncio import create_async_engine

from .canonical_work import CanonicalWorkRepository
from .human_reviews import HumanReviewConsequence, HumanReviewResult, HumanReviewState
from .secure_file import read_private_bytes

MAX_CONSEQUENCE_BYTES = 64 * 1024


class HumanReviewProposer(Protocol):
    async def propose(self, consequence: HumanReviewConsequence) -> HumanReviewResult: ...


def read_consequence(path: Path) -> HumanReviewConsequence:
    """Read one bounded consequence from an exact mode-0600 regular file."""
    payload = read_private_bytes(path)
    if not payload or len(payload) > MAX_CONSEQUENCE_BYTES:
        raise ValueError("consequence JSON must be 1..65536 bytes")
    return HumanReviewConsequence.model_validate_json(payload)


async def propose_file(path: Path, state: HumanReviewProposer) -> HumanReviewResult:
    return await state.propose(read_consequence(path))


async def _propose(path: Path) -> HumanReviewResult:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    try:
        state = HumanReviewState(engine, CanonicalWorkRepository(engine))
        return await propose_file(path, state)
    finally:
        await engine.dispose()


def run(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Prepare one exact Human Review consequence without deciding it."
    )
    parser.add_argument("consequence", type=Path)
    path = parser.parse_args(argv).consequence
    try:
        result = asyncio.run(_propose(path))
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
    print(json.dumps(result.model_dump(mode="json", exclude_none=True), sort_keys=True,
                     separators=(",", ":")))
    if result.status not in {"PREPARED", "REPLAYED"}:
        raise SystemExit(2)
