import argparse
import asyncio
import os

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

from .core import ProviderError, provision_launch
from .provider import AsanaProvider
from .state import PostgresState
from .task_ref import asana_task_id


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Bind canonical Asana tasks to stable, opaque Switchstand WorkIds."
    )
    result.add_argument("--active", required=True, help="active Asana task URL or ID")
    result.add_argument(
        "--reference", action="append", default=[], help="read-only Asana task URL or ID"
    )
    return result


async def run(active: str, references: tuple[str, ...]) -> None:
    database_url = os.environ["DATABASE_URL"]
    token = os.environ["ASANA_TOKEN"]
    engine = create_async_engine(database_url)
    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0",
        trust_env=False,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        authority = await provision_launch(
            PostgresState(engine),
            "asana",
            AsanaProvider(client),
            asana_task_id(active),
            tuple(asana_task_id(value) for value in references),
        )
        print(f"ACTIVE_WORK_ID={authority.active_work_id}")
        print("REFERENCE_WORK_IDS=" + ",".join(map(str, authority.reference_work_ids)))
    finally:
        await client.aclose()
        await engine.dispose()


def upgrade_database() -> None:
    command.upgrade(Config("alembic.ini"), "head")


def main() -> None:
    arguments = parser().parse_args()
    try:
        upgrade_database()
        asyncio.run(run(arguments.active, tuple(arguments.reference)))
    except (KeyError, ValueError, PermissionError, ProviderError) as error:
        parser().exit(1, f"provisioning failed: {error}\n")


if __name__ == "__main__":
    main()
