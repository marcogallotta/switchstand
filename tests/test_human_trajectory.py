import asyncio
import os
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.human_trajectory import (
    HumanTrajectoryData,
    HumanTrajectoryStore,
    SourceKind,
)

WORK_ID = UUID("10000000-0000-4000-8000-000000000001")


def trajectory(*, current: str = "Read-only qualification") -> HumanTrajectoryData:
    return HumanTrajectoryData(
        outcome="Semantic GitHub review and merge",
        settled_decisions=("Postgres is canonical",),
        accepted_cuts_or_deferrals=("Candidate merge follows qualification",),
        unresolved_human_questions=("Choose staging A or B",),
        current_slice=current,
        remaining_outcome="Candidate merge and native enforcement",
        authority_effect_refs=("review:approved-design",),
    )


@pytest.fixture
async def store(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for human-trajectory tests")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, activation_obligation_revisions, "
            "human_review_consequences, "
            "task_run_results, task_run_executions, task_run_requests, priority_claims, "
            "outcome_state_revisions, human_trajectory_revisions, "
            "agent_mailbox_transfer_requests, agent_mailboxes, work_event_handles, "
            "lifecycle_obligations, message_projection, message_deliveries, messages, "
            "effect_intents, work_grants, work_handles CASCADE"
        ))
    await engine.dispose()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO work_handles (id, provider, provider_work_id) "
            "VALUES (:id, 'asana', 'task-1')"
        ), {"id": WORK_ID})
    yield HumanTrajectoryStore(engine)
    await engine.dispose()


def record_args(request_id: UUID, **overrides):
    values = {
        "work_id": WORK_ID,
        "append_request_id": request_id,
        "expected_generation": 1,
        "expected_predecessor_id": None,
        "source_kind": SourceKind.HUMAN_REVIEW,
        "source_ref": "review-delivery-1",
        "source_revision": "revision-1",
        "data": trajectory(),
    }
    values.update(overrides)
    return values


async def test_append_replay_restart_and_stale_check(store):
    request_id = uuid4()
    first = await store.record(**record_args(request_id))
    assert first.status == "APPLIED" and first.revision is not None
    replay = await store.record(**record_args(request_id))
    assert replay.status == "REPLAYED" and replay.revision == first.revision

    restarted = HumanTrajectoryStore(store.engine)
    read = await restarted.get(WORK_ID)
    assert read.status == "CURRENT" and read.head == first.revision
    assert read.head.data.unresolved_human_questions == ("Choose staging A or B",)
    assert (await restarted.check(WORK_ID, first.revision.trajectory_id)).status == "CURRENT"

    successor = await restarted.record(**record_args(
        uuid4(), expected_generation=2,
        expected_predecessor_id=first.revision.trajectory_id,
        source_ref="human-steering-2", source_kind=SourceKind.HUMAN_STEERING,
        data=trajectory(current="Candidate merge"),
    ))
    assert successor.status == "APPLIED" and successor.revision is not None
    check = await restarted.check(WORK_ID, first.revision.trajectory_id)
    assert check.status == "STALE" and check.current_id == successor.revision.trajectory_id
    assert (await restarted.check(WORK_ID, uuid4())).status == "UNKNOWN"


async def test_replay_conflict_and_different_request_after_advance_are_noops(store):
    request_id = uuid4()
    first = await store.record(**record_args(request_id))
    conflict = await store.record(**record_args(
        request_id, data=trajectory(current="Changed replay content")
    ))
    stale = await store.record(**record_args(uuid4()))
    assert conflict.status == "CONFLICT"
    assert stale.status == "STALE" and stale.current_id == first.revision.trajectory_id
    async with store.engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT count(*) FROM human_trajectory_revisions"
        )) == 1


async def test_concurrent_first_writers_serialize_on_work_handle(store):
    first, second = await asyncio.gather(
        store.record(**record_args(uuid4(), source_ref="human-1")),
        store.record(**record_args(uuid4(), source_ref="human-2")),
    )
    assert {first.status, second.status} == {"APPLIED", "STALE"}
    assert (await store.get(WORK_ID)).status == "CURRENT"


async def test_concurrent_successors_have_one_winner(store):
    first = await store.record(**record_args(uuid4()))
    assert first.revision is not None
    shared = {
        "expected_generation": 2,
        "expected_predecessor_id": first.revision.trajectory_id,
    }
    left, right = await asyncio.gather(
        store.record(**record_args(uuid4(), source_ref="left", **shared)),
        store.record(**record_args(uuid4(), source_ref="right", **shared)),
    )
    assert {left.status, right.status} == {"APPLIED", "STALE"}


async def test_corrupt_digest_fails_closed_without_touching_other_state(store):
    before = {}
    async with store.engine.connect() as connection:
        for table in ("effect_intents", "messages", "lifecycle_obligations"):
            before[table] = await connection.scalar(text(f"SELECT count(*) FROM {table}"))
    request_id = uuid4()
    await store.record(**record_args(request_id))
    async with store.engine.begin() as connection:
        await connection.execute(text(
            "UPDATE human_trajectory_revisions SET trajectory_data = "
            "jsonb_set(trajectory_data, '{current_slice}', '\"corrupted\"'::jsonb)"
        ))
    assert (await store.get(WORK_ID)).status == "UNKNOWN"
    assert (await store.record(**record_args(request_id))).status == "UNKNOWN"
    assert (await store.check(WORK_ID, uuid4())).status == "UNKNOWN"
    async with store.engine.connect() as connection:
        for table, count in before.items():
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == count


async def test_populated_migration_refuses_to_discard_trajectory(store):
    await store.record(**record_args(uuid4()))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", os.environ["TEST_DATABASE_URL"])
    with pytest.raises(RuntimeError, match="preserve durable human trajectory truth"):
        command.downgrade(config, "0008_work_index_authority")
    async with store.engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT version_num FROM alembic_version"
        )) == "0025_task_control_checkpoints"
        assert await connection.scalar(text(
            "SELECT count(*) FROM human_trajectory_revisions"
        )) == 1


def test_payload_is_closed_bounded_and_cannot_mint_authorization():
    with pytest.raises(ValidationError, match="implementation_authorization"):
        HumanTrajectoryData.model_validate(
            trajectory().model_dump() | {"implementation_authorization": "AUTHORIZED"}
        )
    with pytest.raises(ValueError, match="1..512"):
        trajectory().model_copy(update={"settled_decisions": ("x" * 513,)}).canonical()
