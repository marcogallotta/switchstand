import argparse
import asyncio
import os

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine

from .canonical_work import CanonicalWorkRepository
from .contracts import LaunchAuthority
from .grant_state import GrantState
from .launch_source import canonical_work, repository_marker
from .managed_identity import rotate_managed_grant


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
        resolved = tuple(
            [await canonical_work(works, active)]
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
