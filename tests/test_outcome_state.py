import asyncio
import os
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.outcome_state import ActionSummary, OutcomeItem, OutcomeStateStore

OWNER = UUID("20000000-0000-4000-8000-000000000001")


def item(description="Choose the rollout"):
    return OutcomeItem(
        item_key="rollout", description=description, kind="DECISION", status="READY",
        who_acts="MARCO", what_yes_causes="Dispatch the reviewed implementation",
    )


@pytest.fixture
async def store(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for outcome-state tests")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, outcome_state_revisions, "
            "work_parent_edges, workset_memberships, worksets, workset_cutovers, "
            "workset_authority, work_edges, work_metadata_cutovers, work_metadata_authority, "
            "human_trajectory_revisions, work_index, work_authority_cutovers, work_authority, "
            "agent_mailboxes, work_event_handles, lifecycle_obligations, message_projection, "
            "message_deliveries, messages, effect_intents, work_grants, work_handles CASCADE"
        ))
    await engine.dispose()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO work_handles (id, provider, provider_work_id) "
            "VALUES (:id, 'asana', 'owner')"
        ), {"id": OWNER})
    yield OutcomeStateStore(engine)
    await engine.dispose()


def args(operation_id, **changes):
    values = {
        "owner_work_id": OWNER, "active_work_id": OWNER, "operation_id": operation_id,
        "expected_state_id": None, "owner_currentness_token": "s1_current", "items": (item(),),
    }
    values.update(changes)
    return values


async def test_owner_cas_replay_concurrency_corruption_and_projection(store):
    operation = uuid4()
    assert (await store.record(**args(operation, active_work_id=uuid4()))).status == "DENIED"
    assert (await store.record(**args(
        uuid4(), items=(item().model_copy(update={"source_label": "MARCO"}),)
    ))).status == "DENIED"
    first, replay = await asyncio.gather(
        store.record(**args(operation)), store.record(**args(operation)),
    )
    assert {first.status, replay.status} == {"APPLIED", "REPLAYED"}
    assert (await store.record(**args(
        operation, items=(item("changed replay"),)
    ))).status == "CONFLICT"
    assert (await store.record(**args(uuid4()))).status == "STALE"
    successor = {"expected_state_id": replay.state_id, "owner_currentness_token": "s1_next"}
    left, right = await asyncio.gather(
        store.record(**args(uuid4(), **successor)), store.record(**args(uuid4(), **successor)),
    )
    assert {left.status, right.status} == {"APPLIED", "STALE"}
    summary = await store.summary(OWNER, "s1_next")
    assert isinstance(summary, ActionSummary)
    assert [action.action_class for action in summary.actions] == ["NEEDS_MARCO"]
    stale = await store.summary(OWNER, "different")
    assert isinstance(stale, ActionSummary)
    assert stale.currentness == "STALE" and stale.open_action_count == 1
    assert [(action.action_class, action.item_key) for action in stale.actions] == [
        ("NEEDS_MARCO", "rollout")
    ]
    head = next(result.state_id for result in (left, right) if result.status == "APPLIED")
    finished = await store.record(**args(
        uuid4(), expected_state_id=head, owner_currentness_token="s1_done",
        items=(item().model_copy(update={"status": "DONE", "what_yes_causes": None}),),
    ))
    assert finished.status == "APPLIED"
    stale_done = await store.summary(OWNER, "later")
    assert isinstance(stale_done, ActionSummary)
    assert stale_done.currentness == "STALE" and stale_done.open_action_count == 0
    assert stale_done.actions == ()
    one, two, collision = uuid4(), uuid4(), uuid4()
    async with store.engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO work_handles (id, provider, provider_work_id) VALUES "
            "(:one, 'asana', 'one'), (:two, 'asana', 'two')"
        ), {"one": one, "two": two})
    cross = await asyncio.gather(
        store.record(**args(collision, owner_work_id=one, active_work_id=one)),
        store.record(**args(collision, owner_work_id=two, active_work_id=two)),
    )
    assert {result.status for result in cross} == {"APPLIED", "CONFLICT"}
    async with store.engine.begin() as connection:
        await connection.execute(text(
            "UPDATE outcome_state_revisions SET items='[]'::jsonb WHERE generation=2"
        ))
    assert await store.summary(OWNER, "s1_next") == "UNKNOWN"
    assert (await store.record(**args(operation))).status == "UNKNOWN"
