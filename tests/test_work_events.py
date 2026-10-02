import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.canonical_work import canonical_metadata, canonical_work
from switchstand.state import metadata as shared_metadata
from switchstand.work_events import (
    OperationConflictError,
    StaleWorkVersion,
    UnknownWorkError,
    WorkEventRepository,
    _decode_cursor,
    _encode_cursor,
    work_events,
)

WORK_ID = UUID("10000000-0000-4000-8000-000000000001")
OTHER_WORK_ID = UUID("20000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


def test_cursor_is_stable_scoped_and_rejects_malformed_values():
    cursor = _encode_cursor(WORK_ID, 17)
    assert cursor == _encode_cursor(WORK_ID, 17)
    assert _decode_cursor(cursor, WORK_ID) == 17
    with pytest.raises(ValueError, match="cursor"):
        _decode_cursor(cursor, OTHER_WORK_ID)
    with pytest.raises(ValueError, match="cursor"):
        _decode_cursor("not-a-cursor", WORK_ID)


def test_inert_event_schema_does_not_join_current_shared_metadata():
    assert canonical_work.metadata is canonical_metadata
    assert work_events.metadata is canonical_metadata
    assert "canonical_work" not in shared_metadata.tables
    assert "work_events" not in shared_metadata.tables


@pytest.fixture
async def repository(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for work-event persistence tests")
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_async_engine(url)
    work_id, other_work_id = uuid4(), uuid4()
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: canonical_metadata.create_all(
            sync, tables=[canonical_work, work_events], checkfirst=True
        ))
        await connection.execute(canonical_work.insert(), [
            {
                "work_id": work_id, "title": "First", "normalized_title": "first",
                "completed": False, "notes": "", "row_version": 3,
            },
            {
                "work_id": other_work_id, "title": "Other", "normalized_title": "other",
                "completed": False, "notes": "", "row_version": 1,
            },
        ])
    yield WorkEventRepository(engine), work_id, other_work_id
    async with engine.begin() as connection:
        await connection.execute(work_events.delete().where(
            work_events.c.work_id.in_((work_id, other_work_id))
        ))
        await connection.execute(canonical_work.delete().where(
            canonical_work.c.work_id.in_((work_id, other_work_id))
        ))
    await engine.dispose()


async def test_append_is_atomic_ordered_and_replay_does_not_advance_version(repository):
    repository, work_id, _ = repository
    operation_id = uuid4()
    first = await repository.append(
        work_id, observed_version=3, operation_id=operation_id,
        subtype="comment_added", text="first", created_at=NOW, actor="Marco",
        asana_story_gid="123",
    )
    assert (
        first.created and first.row_version == 4
        and first.event.sequence == 1 and first.event.result_version == 4
    )

    second = await repository.append(
        work_id, observed_version=4, operation_id=uuid4(),
        subtype="comment_added", text="second", created_at=NOW,
    )
    assert second.event.sequence == 2 and second.row_version == 5
    replay = await WorkEventRepository(repository.engine).append(
        work_id, observed_version=3, operation_id=operation_id,
        subtype="comment_added", text="first", created_at=NOW, actor="Marco",
        asana_story_gid="123",
    )
    assert not replay.created and replay.event == first.event and replay.row_version == 4
    async with repository.engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(work_events).where(
            work_events.c.work_id == work_id
        )) == 2
        assert await connection.scalar(select(canonical_work.c.row_version).where(
            canonical_work.c.work_id == work_id
        )) == 5


async def test_append_rejects_stale_unknown_and_operation_conflict(repository):
    repository, work_id, _ = repository
    operation_id = uuid4()
    await repository.append(
        work_id, observed_version=3, operation_id=operation_id,
        subtype="comment_added", text="original", created_at=NOW,
    )
    with pytest.raises(OperationConflictError):
        await repository.append(
            work_id, observed_version=4, operation_id=operation_id,
            subtype="comment_added", text="changed", created_at=NOW,
        )
    with pytest.raises(StaleWorkVersion) as stale:
        await repository.append(
            work_id, observed_version=3, operation_id=uuid4(),
            subtype="comment_added", text="stale", created_at=NOW,
        )
    assert stale.value.current == 4
    with pytest.raises(UnknownWorkError):
        await repository.append(
            uuid4(), observed_version=1, operation_id=uuid4(),
            subtype="comment_added", text="missing", created_at=NOW,
        )


async def test_history_paging_and_exact_lookup_are_deterministic(repository):
    repository, work_id, other_work_id = repository
    events = []
    version = 3
    for number in range(3):
        outcome = await repository.append(
            work_id, observed_version=version, operation_id=uuid4(),
            subtype="comment_added", text=str(number), created_at=NOW,
        )
        events.append(outcome.event)
        version = outcome.row_version

    first = await repository.page(work_id, limit=2)
    assert first.events == tuple(events[:2]) and first.next_cursor is not None
    second = await repository.page(work_id, cursor=first.next_cursor, limit=2)
    assert second.events == (events[2],) and second.next_cursor is None
    assert await repository.get(work_id, events[1].id) == events[1]
    assert await repository.get(other_work_id, events[1].id) is None
