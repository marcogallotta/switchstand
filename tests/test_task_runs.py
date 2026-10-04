import asyncio
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_revision,
    canonical_work,
)
from switchstand.state import PostgresState
from switchstand.task_runs import (
    AgentTaskRequest,
    TaskRunState,
    task_run_requests,
)


@pytest.fixture
async def subject(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL task-run tests")
    assert make_url(url).database == "switchstand_test"
    sync = create_engine(url)
    with sync.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, task_run_requests, failure_resolutions, "
            "failure_records, work_migration_receipts, "
            "outcome_state_revisions, human_trajectory_revisions, agent_mailboxes, "
            "work_event_handles, lifecycle_obligations, message_projection, message_deliveries, "
            "messages, effect_intents, work_grants, work_handles CASCADE"
        ))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    canonical_work.create(sync, checkfirst=True)
    engine = create_async_engine(url)
    state = PostgresState(engine)
    works = CanonicalWorkRepository(engine)
    requester, execution = uuid4(), uuid4()
    await state.bind_reserved(requester, "local", str(requester))
    await state.bind_reserved(execution, "local", str(execution))
    await works.create(CurrentWork(
        work_id=execution,
        title="Inspect a bounded failure",
        completed=False,
        notes="",
    ))
    yield TaskRunState(engine, works), engine, requester, execution
    await engine.dispose()
    sync.dispose()


def request(execution, **changes):
    values = {
        "api_version": "1",
        "execution_work_id": execution,
        "observed_revision": canonical_revision(execution, 1),
        "task_kind": "INVESTIGATION",
        "objective": "Identify the exact failing invariant.",
        "result_contract": {"required": ["evidence", "conclusion"]},
    }
    return AgentTaskRequest(**(values | changes))


@pytest.mark.parametrize("change", [
    {"task_kind": "IMPLEMENTATION"},
    {"target_runtime": "codex"},
    {"result_contract": {"payload": "x" * 16_385}},
])
def test_public_contract_rejects_implementation_routing_and_unbounded_result(change):
    with pytest.raises(ValidationError):
        request(uuid4(), **change)


async def test_request_is_durable_without_runtime_and_replays_exactly(subject):
    state, engine, requester, execution = subject
    operation_id = uuid4()
    intent = request(execution)

    first = await state.request(requester, operation_id, intent)
    assert first.status == "ok" and first.request is not None
    assert first.request.requester_work_id == requester
    assert first.request.execution_work_id == execution
    assert await state.request(requester, operation_id, intent) == first
    assert await TaskRunState(engine, CanonicalWorkRepository(engine)).get(
        first.request.request_id
    ) == first

    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(task_run_requests)) == 1
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", os.environ["TEST_DATABASE_URL"])
    with pytest.raises(RuntimeError, match="preserve durable task-run evidence"):
        command.downgrade(config, "0013_failure_journal")

    conflict = await state.request(
        requester,
        operation_id,
        intent.model_copy(update={"objective": "A different operation."}),
    )
    assert (conflict.status, conflict.reason) == ("conflict", "operation_identity_conflict")


async def test_request_revision_and_continuation_fail_closed_without_writes(subject):
    state, engine, requester, execution = subject
    unbound_requester = await state.request(uuid4(), uuid4(), request(execution))
    assert (unbound_requester.status, unbound_requester.reason) == (
        "denied", "requester_work_not_found"
    )

    stale = await state.request(
        requester,
        uuid4(),
        request(execution, observed_revision="pg_stale"),
    )
    assert (stale.status, stale.reason) == ("stale", "source_revision_changed")

    continued = await state.request(
        requester,
        uuid4(),
        request(execution, continuation="CONTINUE"),
    )
    assert (continued.status, continued.reason) == ("denied", "continuation_not_bound")

    missing = uuid4()
    absent = await state.request(requester, uuid4(), request(missing))
    assert (absent.status, absent.reason) == ("denied", "execution_work_not_found")
    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(task_run_requests)) == 0


async def test_concurrent_exact_replay_creates_one_request(subject):
    state, engine, requester, execution = subject
    operation_id = uuid4()
    intent = request(execution, task_kind="VALIDATION")
    results = await asyncio.gather(*(
        state.request(requester, operation_id, intent) for _ in range(4)
    ))
    assert all(result.status == "ok" for result in results)
    assert len({result.request.request_id for result in results if result.request}) == 1
    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(task_run_requests)) == 1
