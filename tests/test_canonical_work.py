import os
from collections.abc import AsyncGenerator
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.canonical_work import CanonicalWorkRepository, CurrentWork, normalize_title
from switchstand.state import canonical_work, legacy_work_aliases, metadata


@pytest.fixture
async def repository(database_prerequisite: None) -> AsyncGenerator[CanonicalWorkRepository]:
    del database_prerequisite
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: metadata.drop_all(
            sync, tables=[legacy_work_aliases, canonical_work], checkfirst=True
        ))
        await connection.run_sync(lambda sync: metadata.create_all(
            sync, tables=[canonical_work, legacy_work_aliases]
        ))
    yield CanonicalWorkRepository(engine)
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: metadata.drop_all(
            sync, tables=[legacy_work_aliases, canonical_work]
        ))
    await engine.dispose()


def test_compact_schema_has_no_provider_or_authority_columns():
    assert canonical_work.primary_key.columns.keys() == ["work_id"]
    assert set(canonical_work.c.keys()) == {
        "work_id", "title", "normalized_title", "completed", "notes", "priority",
        "work_type", "lifecycle_state", "review_next_action", "wait_kind",
        "unblock_condition", "next_due", "row_version",
    }
    assert legacy_work_aliases.primary_key.columns.keys() == ["asana_task_gid"]
    assert normalize_title("  Café  TASK ") == "café task"


async def test_real_postgres_create_get_search_replace_alias_and_stale_rollback(
    repository: CanonicalWorkRepository,
) -> None:
    first_id, second_id = uuid4(), uuid4()
    first = CurrentWork(
        first_id, "Alpha Task", False, "notes", priority="P0",
        lifecycle_state="CURRENT", wait_kind="NONE", unblock_condition="NONE",
        next_due="NONE",
    )
    second = CurrentWork(second_id, "Beta Task", True, "done", lifecycle_state="TERMINAL")
    await repository.create(first)
    await repository.create(second)
    await repository.bind_asana_gid("1218000000000001", first_id)

    assert await repository.get(first_id) == first
    assert await repository.resolve_asana_gid("1218000000000001") == first_id
    assert await repository.resolve_asana_gid("missing") is None
    assert await repository.search("  ALPHA ") == (first,)
    assert await repository.search(completed=True) == (second,)

    changed = await repository.replace(replace(
        first, title="Gamma Task", notes="changed", completed=True,
        lifecycle_state="TERMINAL",
    ))
    assert changed.row_version == 2
    assert await CanonicalWorkRepository(repository.engine).get(first_id) == changed
    assert await repository.search("gamma", completed=True) == (changed,)

    with pytest.raises(ValueError, match="stale"):
        await repository.replace(replace(first, title="must roll back"))
    assert await repository.get(first_id) == changed

    with pytest.raises(IntegrityError):
        await repository.bind_asana_gid("1218000000000001", second_id)
    assert await repository.resolve_asana_gid("1218000000000001") == first_id
