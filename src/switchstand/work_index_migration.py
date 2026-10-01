"""One-shot offline Stage 1 import and irreversible authority flip."""

from __future__ import annotations

import argparse
import asyncio
import os

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .core import ProviderError
from .discovery import DiscoveryProvider, ProviderSearchItem
from .provider import AsanaProvider
from .work_index import ActivationReceipt, ActivationUnknown, activate


async def final_scan(provider: DiscoveryProvider) -> tuple[ProviderSearchItem, ...]:
    """Read the entire admitted provider corpus once; retain nothing on failure."""
    cursor: str | None = None
    cursors: set[str] = set()
    items: list[ProviderSearchItem] = []
    seen: set[str] = set()
    while True:
        page = await provider.search_work(None, None, cursor, 100)
        for item in page.items:
            if item.provider_work_id in seen:
                raise ValueError("provider final scan returned duplicate admitted work")
            seen.add(item.provider_work_id)
            items.append(item)
        cursor = page.next_cursor
        if cursor is None:
            break
        if cursor in cursors:
            raise ValueError("provider final scan repeated a cursor")
        cursors.add(cursor)
    if not items:
        raise ValueError("provider final scan returned no admitted work")
    return tuple(items)


async def require_offline(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        others = await connection.scalar(text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND backend_type = 'client backend'"
        ))
    if others:
        raise RuntimeError("Stage 1 requires the MCP service and every other DB client stopped")


async def migrate(*, confirm_offline: bool, expected_manifest_digest: str) -> ActivationReceipt:
    if not confirm_offline:
        raise ValueError("explicit --confirm-offline is required")
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", trust_env=False,
        headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"},
    )
    try:
        await require_offline(engine)
        provider = AsanaProvider(client, os.getenv("SWITCHSTAND_TEST_PROJECT_GID"))
        items = await final_scan(provider)
        await require_offline(engine)
        return await activate(
            engine, items, expected_manifest_digest=expected_manifest_digest
        )
    finally:
        await client.aclose()
        await engine.dispose()


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-offline", action="store_true")
    parser.add_argument("--expected-manifest-digest", required=True)
    arguments = parser.parse_args(argv)
    try:
        receipt = asyncio.run(migrate(
            confirm_offline=arguments.confirm_offline,
            expected_manifest_digest=arguments.expected_manifest_digest,
        ))
    except ActivationUnknown as error:
        parser.exit(2, f"{error}\n")
    except (KeyError, ProviderError, RuntimeError, SQLAlchemyError, ValueError) as error:
        parser.exit(1, f"Stage 1 migration failed before authority flip: {error}\n")
    print(
        f"POSTGRES_AUTHORITY active generation={receipt.generation} count={receipt.count} "
        f"corpus_sha256={receipt.corpus_digest} "
        f"recovered_after_commit_error={str(receipt.recovered_after_commit_error).lower()}"
    )


if __name__ == "__main__":
    run()
