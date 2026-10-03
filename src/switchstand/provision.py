import argparse
import asyncio
import os
from pathlib import Path
from uuid import UUID

import httpx
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine

from .bootstrap_adapters import (
    AuthenticatedMigrationReceiptFile,
    ExactAsanaTaskProbe,
    ExistingGrantAdapter,
    PostgresIdentityMapping,
)
from .bootstrap_identity import resolve_and_admit_pre_migration_launch
from .canonical_work import CanonicalWorkRepository
from .contracts import LaunchAuthority
from .grant_state import GrantState
from .launch_source import canonical_work, repository_marker
from .managed_identity import managed_principal, rotate_managed_grant
from .provider import AsanaProvider
from .state import PostgresState


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Resolve canonical work into one managed launch authority."
    )
    result.add_argument("--active", required=True, help="active WorkId or legacy task ID/URL")
    result.add_argument(
        "--reference", action="append", default=[], help="read-only WorkId or legacy task ID/URL"
    )
    result.add_argument("--managed-agent", action="store_true")
    result.add_argument("--repository", action="store_true")
    return result


async def run(
    active: str, references: tuple[str, ...], *, managed_agent: bool = False, repository: bool = False,
) -> None:
    if len(references) > 8:
        raise ValueError("at most eight reference tasks are allowed")
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        works = CanonicalWorkRepository(engine)
        exact_active: UUID | None
        try:
            exact_active = UUID(active) if str(UUID(active)) == active else None
        except ValueError:
            exact_active = None
        canonical_active = None if exact_active is None else await works.get(exact_active)
        if exact_active is not None and canonical_active is None:
            if references or repository:
                raise ValueError("pre-migration bootstrap supports one local work target only")
            async with httpx.AsyncClient(
                base_url="https://app.asana.com/api/1.0",
                headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"},
            ) as client:
                admitted = await resolve_and_admit_pre_migration_launch(
                    active,
                    PostgresIdentityMapping(PostgresState(engine)),
                    ExactAsanaTaskProbe(AsanaProvider(client)),
                    AuthenticatedMigrationReceiptFile(
                        Path(os.environ["SWITCHSTAND_MIGRATION_RECEIPT"])
                        if os.getenv("SWITCHSTAND_MIGRATION_RECEIPT") else None,
                        os.getenv("SWITCHSTAND_MIGRATION_RECEIPT_SHA256"),
                    ),
                    managed_principal(exact_active),
                    ExistingGrantAdapter(GrantState(engine)),
                )
            authority = admitted.grant.authority
            print(f"ACTIVE_WORK_ID={authority.active_work_id}")
            print("REFERENCE_WORK_IDS=" + ",".join(map(str, authority.reference_work_ids)))
            print(f"LEGACY_TASK_GIDS={admitted.identity.provider_work_id}")
            return
        resolved = tuple(
            [canonical_active or await canonical_work(works, active)]
            + [await canonical_work(works, value) for value in references]
        )
        if len({work.work_id for work in resolved}) != len(resolved):
            raise ValueError("active and reference tasks must be distinct")
        authority = LaunchAuthority(
            active_work_id=resolved[0].work_id,
            reference_work_ids=tuple(work.work_id for work in resolved[1:]),
        )
        if repository:
            slug = repository_marker(resolved[0].notes)
            if slug != "marcogallotta/ai-tools":
                raise ValueError("repository is outside the prototype allowlist")
            print(f"SWITCHSTAND_REPOSITORY={slug}")
        if managed_agent:
            await rotate_managed_grant(GrantState(engine), authority)
        print(f"ACTIVE_WORK_ID={authority.active_work_id}")
        print("REFERENCE_WORK_IDS=" + ",".join(map(str, authority.reference_work_ids)))
        print("LEGACY_TASK_GIDS=" + ",".join(
            await works.asana_gids(authority.active_work_id)
        ))
    finally:
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
            arguments.active, tuple(arguments.reference), managed_agent=arguments.managed_agent,
            repository=arguments.repository
        ))
    except (KeyError, ValueError, PermissionError, RuntimeError) as error:
        parser().exit(1, f"provisioning failed: {error}\n")


if __name__ == "__main__":
    main()
