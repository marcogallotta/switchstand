import argparse
import asyncio
import os

import httpx
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine

from .core import ProviderError, provision_launch
from .grant_state import GrantState
from .managed_identity import rotate_managed_grant
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
    result.add_argument(
        "--managed-agent", action="store_true",
        help="rotate the trusted durable task-bound managed-agent grant",
    )
    return result


async def run(
    active: str, references: tuple[str, ...], *, managed_agent: bool = False,
) -> None:
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
            AsanaProvider(client, os.getenv("SWITCHSTAND_TEST_PROJECT_GID")),
            asana_task_id(active),
            tuple(asana_task_id(value) for value in references),
        )
        if managed_agent:
            await rotate_managed_grant(GrantState(engine), authority)
        print(f"ACTIVE_WORK_ID={authority.active_work_id}")
        print("REFERENCE_WORK_IDS=" + ",".join(map(str, authority.reference_work_ids)))
    finally:
        await client.aclose()
        await engine.dispose()


def migration_config(root: str = ".") -> Config:
    config = Config(os.path.join(root, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(root, "migrations"))
    return config


def require_current_schema(root: str = ".") -> None:
    config = migration_config(root)
    expected = set(ScriptDirectory.from_config(config).get_heads())
    engine = create_engine(os.environ["DATABASE_URL"])
    try:
        with engine.connect() as connection:
            current = set(MigrationContext.configure(connection).get_current_heads())
    finally:
        engine.dispose()
    if current != expected:
        expected_text = ",".join(sorted(expected)) or "<none>"
        current_text = ",".join(sorted(current)) or "<none>"
        raise RuntimeError(
            f"shared CONTROL schema mismatch: expected {expected_text}; "
            f"actual {current_text}"
        )


def upgrade_database(root: str = ".") -> None:
    command.upgrade(migration_config(root), "head")


def main() -> None:
    arguments = parser().parse_args()
    try:
        require_current_schema()
        asyncio.run(run(
            arguments.active, tuple(arguments.reference), managed_agent=arguments.managed_agent
        ))
    except (KeyError, ValueError, PermissionError, ProviderError, RuntimeError) as error:
        parser().exit(1, f"provisioning failed: {error}\n")


if __name__ == "__main__":
    main()
