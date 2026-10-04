import hashlib
import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.bootstrap_identity import MIGRATION_COMPLETE_RECEIPT
from switchstand.canonical_relations import project_memberships, projects
from switchstand.canonical_work import canonical_metadata, canonical_work
from switchstand.state import metadata, work_migration_receipts
from switchstand.work_corpus import (
    compare_parity_exports,
    parity_manifest,
    parity_value,
    write_manifest,
)
from switchstand.work_events import work_events
from switchstand.work_import import (
    TABLES,
    import_parity,
    import_parity_connection,
    target_parity,
)

WORK = UUID("10000000-0000-4000-8000-000000000001")
PARENT = UUID("20000000-0000-4000-8000-000000000002")
PROJECT = UUID("30000000-0000-4000-8000-000000000003")
EVENT = UUID("40000000-0000-4000-8000-000000000004")
NOW = datetime(2026, 10, 2, 12, 30, tzinfo=UTC)


def record(kind: str, identity: list[str], fields: dict[str, object]) -> dict[str, object]:
    import json

    return {
        "kind": kind,
        "id": json.dumps(identity, separators=(",", ":")),
        "fields": fields,
    }


def work(work_id: UUID, title: str) -> dict[str, object]:
    return record("work", [str(work_id)], {
        "work_id": str(work_id), "title": title, "normalized_title": title.casefold(),
        "completed": False, "notes": f"notes for {title}", "assignee": "Marco",
        "priority": "P1", "work_type": "Implementation", "lifecycle_state": None,
        "review_next_action": None,
        "canonical_root": str(PARENT) if work_id == WORK else None,
        "owner_key": "coordinator" if work_id == WORK else None,
        "wait_kind": None, "unblock_condition": None, "next_due": None,
        "next_action_class": "OWNER_CAN_DO" if work_id == WORK else None,
        "next_action_ref": "implement" if work_id == WORK else None,
        "row_version": 1,
    })


def source_records() -> list[dict[str, object]]:
    return [
        work(WORK, "Child"), work(PARENT, "Parent"),
        record("alias", ["1218000000000001"], {
            "asana_task_gid": "1218000000000001", "work_id": str(WORK),
        }),
        record("project", [str(PROJECT)], {
            "project_id": str(PROJECT), "asana_project_gid": "1218000000000002",
            "name": "Current",
        }),
        record("parent", [str(WORK)], {
            "child_work_id": str(WORK), "parent_work_id": str(PARENT),
        }),
        record("dependency", [str(WORK), str(PARENT)], {
            "work_id": str(WORK), "depends_on_work_id": str(PARENT),
        }),
        record("membership", [str(PROJECT), str(WORK)], {
            "project_id": str(PROJECT), "work_id": str(WORK), "section_name": "Doing",
        }),
        record("event", [str(EVENT)], {
            "id": str(EVENT), "work_id": str(WORK), "sequence": 1,
            "result_version": 1, "subtype": "comment_added", "text": "hello",
            "created_at": parity_value(NOW), "actor": "Marco", "asana_story_gid": "987",
            "operation_id": None,
        }),
    ]


@pytest.fixture
async def engine(database_prerequisite: None) -> AsyncGenerator[AsyncEngine]:
    del database_prerequisite
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.execute(text("TRUNCATE work_handles CASCADE"))
        await connection.run_sync(canonical_metadata.drop_all)
        await connection.run_sync(canonical_metadata.create_all)
    yield engine
    async with engine.begin() as connection:
        await connection.run_sync(canonical_metadata.drop_all)
    await engine.dispose()


async def test_one_shot_import_and_exact_target_export(
    engine: AsyncEngine, tmp_path: Path,
):
    source = tmp_path / "source.json"
    target = tmp_path / "target.json"
    write_manifest(source, parity_manifest(source_records()))

    assert await import_parity(engine, source) == 8
    write_manifest(target, await target_parity(engine))

    result = compare_parity_exports(source, target)
    assert result.records == 8
    async with engine.connect() as connection:
        assert await connection.scalar(select(canonical_work.c.notes).where(
            canonical_work.c.work_id == WORK
        )) == "notes for Child"
        assert (await connection.execute(select(
            canonical_work.c.canonical_root,
            canonical_work.c.owner_key,
            canonical_work.c.next_action_class,
            canonical_work.c.next_action_ref,
        ).where(canonical_work.c.work_id == WORK))).one() == (
            str(PARENT), "coordinator", "OWNER_CAN_DO", "implement",
        )
        assert (await connection.execute(select(
            canonical_work.c.canonical_root,
            canonical_work.c.owner_key,
            canonical_work.c.next_action_class,
            canonical_work.c.next_action_ref,
        ).where(canonical_work.c.work_id == PARENT))).one() == (None, None, None, None)
        assert await connection.scalar(select(projects.c.asana_project_gid)) == (
            "1218000000000002"
        )
        assert await connection.scalar(select(project_memberships.c.section_name)) == "Doing"
        assert await connection.scalar(select(work_events.c.id)) == EVENT

    with pytest.raises(ValueError, match="not empty"):
        await import_parity(engine, source)


async def test_reordered_source_import_records_receipt_only_after_exact_readback(
    engine: AsyncEngine, tmp_path: Path,
):
    source = tmp_path / "reordered.json"
    records = list(reversed(source_records()))
    write_manifest(source, parity_manifest(records))

    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(
            work_migration_receipts
        )) == 0

    assert await import_parity(engine, source) == len(records)

    async with engine.connect() as connection:
        assert (await connection.execute(select(
            work_migration_receipts.c.name,
            work_migration_receipts.c.source_digest,
        ))).one() == (
            MIGRATION_COMPLETE_RECEIPT,
            hashlib.sha256(source.read_bytes()).hexdigest(),
        )


async def test_connection_import_is_owned_by_caller_transaction(
    engine: AsyncEngine, tmp_path: Path,
):
    source = tmp_path / "rollback.json"
    write_manifest(source, parity_manifest(source_records()))

    async with engine.connect() as connection:
        transaction = await connection.begin()
        assert await import_parity_connection(connection, source) == len(source_records())
        assert await connection.scalar(select(func.count()).select_from(
            work_migration_receipts
        )) == 1
        await transaction.rollback()

    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(canonical_work)) == 0
        assert await connection.scalar(select(func.count()).select_from(
            work_migration_receipts
        )) == 0


async def test_readback_mismatch_rolls_back_without_migration_receipt(
    engine: AsyncEngine, tmp_path: Path,
):
    source = tmp_path / "mismatch.json"
    write_manifest(source, parity_manifest(source_records()))
    async with engine.begin() as connection:
        await connection.execute(text("""
            CREATE FUNCTION switchstand_test_rewrite_import_notes() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                NEW.notes := NEW.notes || '-changed';
                RETURN NEW;
            END
            $$
        """))
        await connection.execute(text("""
            CREATE TRIGGER switchstand_test_rewrite_import_notes
            BEFORE INSERT ON canonical_work
            FOR EACH ROW EXECUTE FUNCTION switchstand_test_rewrite_import_notes()
        """))
    try:
        with pytest.raises(ValueError, match="target readback does not match source parity"):
            await import_parity(engine, source)

        async with engine.connect() as connection:
            assert await connection.scalar(select(func.count()).select_from(
                work_migration_receipts
            )) == 0
            assert await connection.scalar(select(func.count()).select_from(
                canonical_work
            )) == 0
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(
                "DROP TRIGGER IF EXISTS switchstand_test_rewrite_import_notes "
                "ON canonical_work"
            ))
            await connection.execute(text(
                "DROP FUNCTION IF EXISTS switchstand_test_rewrite_import_notes()"
            ))


async def test_target_parity_normalizes_non_utc_postgres_session(
    engine: AsyncEngine, tmp_path: Path,
):
    source = tmp_path / "source.json"
    write_manifest(source, parity_manifest(source_records()))
    await import_parity(engine, source)

    url = os.environ["TEST_DATABASE_URL"]
    non_utc = create_async_engine(
        url, connect_args={"options": "-c timezone=Europe/Rome"},
    )
    try:
        target = tmp_path / "target.json"
        write_manifest(target, await target_parity(non_utc))
        assert compare_parity_exports(source, target).records == 8
    finally:
        await non_utc.dispose()


async def test_invalid_record_rolls_back_the_whole_import(
    engine: AsyncEngine, tmp_path: Path,
):
    records = source_records()
    membership = next(row for row in records if row["kind"] == "membership")
    missing = UUID("50000000-0000-4000-8000-000000000005")
    fields = membership["fields"]
    assert isinstance(fields, dict)
    fields["work_id"] = str(missing)
    membership["id"] = f'["{PROJECT}","{missing}"]'
    source = tmp_path / "invalid.json"
    write_manifest(source, parity_manifest(records))

    with pytest.raises(IntegrityError):
        await import_parity(engine, source)

    async with engine.connect() as connection:
        for _, table, _ in TABLES:
            assert await connection.scalar(select(func.count()).select_from(table)) == 0


async def test_import_rejects_identity_or_schema_drift_before_writing(
    engine: AsyncEngine, tmp_path: Path,
):
    records = source_records()
    records[0]["id"] = '["wrong"]'
    source = tmp_path / "wrong-identity.json"
    write_manifest(source, parity_manifest(records))
    with pytest.raises(ValueError, match="identity"):
        await import_parity(engine, source)

    records = source_records()
    fields = records[0]["fields"]
    assert isinstance(fields, dict)
    fields["unexpected"] = "value"
    source = tmp_path / "wrong-schema.json"
    write_manifest(source, parity_manifest(records))
    with pytest.raises(ValueError, match="columns"):
        await import_parity(engine, source)


async def test_import_rejects_empty_manifest(engine: AsyncEngine, tmp_path: Path):
    source = tmp_path / "empty.json"
    write_manifest(source, parity_manifest([]))

    with pytest.raises(ValueError, match="no canonical work"):
        await import_parity(engine, source)
