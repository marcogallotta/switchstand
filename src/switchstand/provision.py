import argparse
import asyncio
import hashlib
import json
import os
import sys
from typing import cast
from uuid import UUID

import httpx
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine

from .bootstrap_adapters import (
    ExactAsanaTaskProbe,
    ExistingGrantAdapter,
    PostgresIdentityMapping,
    PostgresMigrationReceiptReader,
)
from .bootstrap_identity import resolve_and_admit_pre_migration_launch
from .canonical_work import CanonicalWorkRepository
from .contracts import LaunchAuthority
from .failure_journal import FailureJournal, FailureRecord, redact_environment
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
        replay_lines = sys.stdin if os.getenv("SWITCHSTAND_FAILURE_REPLAY") == "1" else ()
        for line in replay_lines:
            frame_value: object = json.loads(line)
            if not isinstance(frame_value, dict):
                raise TypeError("invalid failure replay frame")
            untyped = cast(dict[object, object], frame_value)
            if set(untyped) != {"record", "digest"}:
                raise ValueError("invalid failure replay frame")
            frame = cast(dict[str, object], untyped)
            record = FailureRecord.model_validate(frame["record"])
            encoded = json.dumps(
                record.canonical(), sort_keys=True, separators=(",", ":")
            ).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            if frame["digest"] != digest:
                raise ValueError("failure replay digest mismatch")
            journal = FailureJournal(engine)
            result = await journal.record(record)
            if result.status not in {"APPLIED", "REPLAYED"} or await journal.get(
                record.attempt_id
            ) != record:
                raise RuntimeError("failure replay readback mismatch")
            print("FAILURE_ACK=" + json.dumps({
                "attempt_id": str(record.attempt_id),
                "operation_id": str(record.operation_id),
                "digest": digest,
            }, sort_keys=True, separators=(",", ":")))
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
                    PostgresMigrationReceiptReader(engine),
                    managed_principal(exact_active),
                    ExistingGrantAdapter(GrantState(engine)),
                )
            authority = admitted.grant.authority
            print(f"ACTIVE_WORK_ID={authority.active_work_id}")
            print("REFERENCE_WORK_IDS=" + ",".join(map(str, authority.reference_work_ids)))
            print(f"LEGACY_TASK_GIDS={admitted.identity.provider_work_id}")
            print(f"MANAGED_GRANT_ID={admitted.grant.id}")
            print(f"MANAGED_GRANT_VERSION={admitted.grant.version}")
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
        grant = await rotate_managed_grant(GrantState(engine), authority) if managed_agent else None
        print(f"ACTIVE_WORK_ID={authority.active_work_id}")
        print("REFERENCE_WORK_IDS=" + ",".join(map(str, authority.reference_work_ids)))
        print("LEGACY_TASK_GIDS=" + ",".join(
            await works.asana_gids(authority.active_work_id)
        ))
        if grant is not None:
            print(f"MANAGED_GRANT_ID={grant.id}")
            print(f"MANAGED_GRANT_VERSION={grant.version}")
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
    except Exception as error:  # noqa: BLE001 - this is the final secret-redacting CLI boundary
        detail = redact_environment(str(error), dict(os.environ))
        parser().exit(1, f"provisioning failed: {detail[:4000]}\n")


if __name__ == "__main__":
    main()
