import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.failure_journal import (
    EffectState,
    FailureJournal,
    FailureRecord,
    FailureResolution,
)

NOW = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
WORK_ID = "20000000-0000-4000-8000-000000000001"


def failure(*, attempt_id=None, operation_id=None, result="connection refused"):
    return FailureRecord(
        attempt_id=attempt_id or uuid4(),
        operation_id=operation_id or uuid4(),
        attempted_claim="start managed worker",
        observed_result=result,
        effect_state=EffectState.NOT_SENT,
        owner=WORK_ID,
        occurred_at=NOW,
        evidence=("logs/worker.err",),
    )


def test_validation_and_redaction():
    with pytest.raises(ValidationError):
        failure().model_copy(update={"owner": "not-a-work-id"}).model_validate(
            {**failure().model_dump(), "owner": "not-a-work-id"}
        )
    with pytest.raises(ValidationError):
        FailureRecord(**{**failure().model_dump(), "occurred_at": NOW.replace(tzinfo=None)})

    value = failure(result="Bearer abc.def password=hunter2 postgresql://user:pass@db/x")
    sanitized = value.sanitized()
    assert "abc.def" not in sanitized.observed_result
    assert "hunter2" not in sanitized.observed_result
    assert "user:pass" not in sanitized.observed_result


@pytest.fixture
async def journal(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for failure journal tests")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await engine.dispose()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_async_engine(url)
    yield FailureJournal(engine)
    await engine.dispose()


async def test_canonical_replay_conflict_resolution_and_open_reads(journal):
    value = failure()
    assert (await journal.record(value)).status == "APPLIED"
    assert (await journal.record(value)).status == "REPLAYED"
    assert (
        await journal.record(value.model_copy(update={"observed_result": "different"}))
    ).status == "CONFLICT"
    assert await journal.get(value.attempt_id) == value
    assert await journal.attempt_ids() == (value.attempt_id,)
    assert await journal.open(owner=WORK_ID) == (value,)

    resolution = FailureResolution(
        resolution_id=uuid4(),
        operation_id=uuid4(),
        attempt_id=value.attempt_id,
        resolved_at=NOW,
        summary="fixed",
        evidence=("commit:abc",),
    )
    assert (await journal.resolve(resolution)).status == "APPLIED"
    assert (await journal.resolve(resolution)).status == "REPLAYED"
    assert await journal.open(owner=WORK_ID) == ()

    foreign = failure(attempt_id=uuid4(), operation_id=value.operation_id)
    assert (await journal.record(foreign)).status == "CONFLICT"
