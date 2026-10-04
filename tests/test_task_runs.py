import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from mcp.server.mcpserver.exceptions import ToolError
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
from switchstand.contracts import LaunchAuthority
from switchstand.grant_state import GrantState
from switchstand.managed_identity import managed_principal, rotate_managed_grant
from switchstand.mcp import build_server
from switchstand.run import (
    RECEIPT,
    RunReceipt,
    managed_runtime_currentness,
    process_start_token,
)
from switchstand.state import PostgresState
from switchstand.task_runs import (
    AgentTaskRequest,
    TaskRunState,
    task_run_executions,
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
            "DROP TABLE IF EXISTS alembic_version, task_run_results, task_run_executions, "
            "task_run_requests, failure_resolutions, "
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


def receipt(execution, run_id=None):
    return RunReceipt(
        run_id=run_id or uuid4(),
        active_work_id=execution,
        worktree="/tmp/task-run-test",
        branch="test",
        pid=1,
        start_token=1,
        started_at=datetime.now(UTC),
    )


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


async def test_start_bind_is_trusted_exact_and_one_to_one(subject):
    state, engine, requester, execution = subject
    first = await state.request(requester, uuid4(), request(execution))
    second = await state.request(requester, uuid4(), request(execution))
    assert first.request is not None and second.request is not None

    missing = await state.bind_start(uuid4(), receipt(execution))
    assert (missing.status, missing.reason) == ("denied", "request_not_found")
    wrong_work = receipt(uuid4())
    denied = await state.bind_start(first.request.request_id, wrong_work)
    assert (denied.status, denied.reason) == ("denied", "execution_work_mismatch")

    run = receipt(execution)
    bound = await state.bind_start(first.request.request_id, run)
    assert bound.status == "ok" and bound.execution is not None
    assert bound.execution.run_id == run.run_id
    assert await state.bind_start(first.request.request_id, run) == bound

    replacement = await state.bind_start(first.request.request_id, receipt(execution))
    assert (replacement.status, replacement.reason) == (
        "denied", "execution_already_bound"
    )
    reused = await state.bind_start(second.request.request_id, run)
    assert (reused.status, reused.reason) == ("conflict", "run_already_bound")
    async with engine.connect() as connection:
        assert await connection.scalar(
            select(func.count()).select_from(task_run_executions)
        ) == 1
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", os.environ["TEST_DATABASE_URL"])
    with pytest.raises(RuntimeError, match="preserve durable task-run execution evidence"):
        command.downgrade(config, "0014_task_runs")


async def test_concurrent_distinct_runs_bind_one_execution(subject):
    state, engine, requester, execution = subject
    requested = await state.request(requester, uuid4(), request(execution))
    assert requested.request is not None

    outcomes = await asyncio.gather(*(
        state.bind_start(requested.request.request_id, receipt(execution))
        for _ in range(2)
    ))

    assert sorted((item.status, item.reason) for item in outcomes) == [
        ("denied", "execution_already_bound"),
        ("ok", None),
    ]
    async with engine.connect() as connection:
        assert await connection.scalar(
            select(func.count()).select_from(task_run_executions)
        ) == 1


async def test_concurrent_shared_run_binds_one_request(subject):
    state, engine, requester, execution = subject
    requests = await asyncio.gather(*(
        state.request(requester, uuid4(), request(execution))
        for _ in range(2)
    ))
    assert all(item.request is not None for item in requests)
    request_ids = [item.request.request_id for item in requests if item.request]
    run = receipt(execution)

    outcomes = await asyncio.gather(*(
        state.bind_start(request_id, run) for request_id in request_ids
    ))

    assert sorted((item.status, item.reason) for item in outcomes) == [
        ("conflict", "run_already_bound"),
        ("ok", None),
    ]
    async with engine.connect() as connection:
        assert await connection.scalar(
            select(func.count()).select_from(task_run_executions)
        ) == 1


async def test_managed_request_uses_exact_launch_grant_receipt_and_semantic_replay(
    subject, tmp_path: Path,
):
    state, engine, requester, execution = subject
    await CanonicalWorkRepository(engine).create(CurrentWork(
        work_id=requester,
        title="Validate the active managed assignment",
        completed=False,
        notes="",
    ))
    grants = GrantState(engine)
    principal = managed_principal(requester)
    repo, git_dir = tmp_path / "repo", tmp_path / "git"
    repo.mkdir()
    git_dir.mkdir()
    run = RunReceipt(
        run_id=uuid4(),
        active_work_id=requester,
        worktree=str(repo),
        branch="task-request",
        pid=os.getpid(),
        start_token=process_start_token(os.getpid()),
        started_at=datetime.now(UTC),
    )
    (git_dir / RECEIPT).write_text(run.model_dump_json() + "\n")
    presented = [run.run_id]
    available = [True]

    def currentness():
        if not available[0]:
            return None
        return managed_runtime_currentness(
            str(presented[0]), requester, repo, run.branch, git_dir,
        )

    server = build_server(
        object(), requester, grants=grants, principal=principal,
        currentness=currentness, task_runs=state,
    )
    intent = request(execution)
    arguments = intent.model_dump(mode="json")

    absent = await server.call_tool("agent_task_request", arguments)
    assert absent.structured_content == {
        "status": "denied", "request": None, "reason": "no_current_grant",
    }

    granted = await rotate_managed_grant(
        grants, LaunchAuthority(active_work_id=requester)
    )
    without_operation = granted.model_copy(update={
        "id": uuid4(),
        "version": granted.version + 1,
        "operations": frozenset({"work_get"}),
    })
    await grants.issue(without_operation, granted.version)
    denied = await server.call_tool("agent_task_request", arguments)
    assert denied.structured_content == {
        "status": "denied", "request": None, "reason": "operation_not_granted",
    }

    wrong_work = without_operation.model_copy(update={
        "id": uuid4(),
        "version": without_operation.version + 1,
        "authority": LaunchAuthority(active_work_id=uuid4()),
        "operations": frozenset({"agent_task"}),
    })
    await grants.issue(wrong_work, without_operation.version)
    denied = await server.call_tool("agent_task_request", arguments)
    assert denied.structured_content == {
        "status": "denied", "request": None, "reason": "operation_not_granted",
    }

    default_grant = await rotate_managed_grant(
        grants, LaunchAuthority(active_work_id=requester)
    )
    assert "agent_task" not in default_grant.operations
    current = default_grant.model_copy(update={
        "id": uuid4(),
        "version": default_grant.version + 1,
        "operations": default_grant.operations | frozenset({"agent_task"}),
    })
    await grants.issue(current, default_grant.version)
    assert "agent_task" in current.operations
    denied = await server.call_tool("agent_task_request", arguments)
    assert denied.structured_content == {
        "status": "denied", "request": None, "reason": "operation_not_granted",
    }
    async with engine.connect() as connection:
        assert await connection.scalar(
            select(func.count()).select_from(task_run_requests)
        ) == 0

    arguments = request(requester).model_dump(mode="json")
    presented[0] = uuid4()
    stale = await server.call_tool("agent_task_request", arguments)
    assert stale.structured_content == {
        "status": "stale", "request": None, "reason": "requester_run_superseded",
    }
    available[0] = False
    unknown = await server.call_tool("agent_task_request", arguments)
    assert unknown.structured_content == {
        "status": "unknown", "request": None,
        "reason": "runtime_currentness_unavailable",
    }

    available[0] = True
    presented[0] = run.run_id
    first = await server.call_tool("agent_task_request", arguments)
    replay = await server.call_tool("agent_task_request", arguments)
    assert not first.is_error and replay.structured_content == first.structured_content
    assert first.structured_content["status"] == "ok"
    assert first.structured_content["request"]["requester_work_id"] == str(requester)
    changed = await server.call_tool(
        "agent_task_request",
        arguments | {"objective": "Validate a distinct semantic basis."},
    )
    assert changed.structured_content["status"] == "ok"
    assert changed.structured_content["request"]["request_id"] != (
        first.structured_content["request"]["request_id"]
    )
    async with engine.connect() as connection:
        assert await connection.scalar(
            select(func.count()).select_from(task_run_requests)
        ) == 2
        assert await connection.scalar(
            select(func.count()).select_from(task_run_executions)
        ) == 0

    for forbidden in (
        {"operation_id": str(uuid4())},
        {"requester_work_id": str(requester)},
        {"runtime": "codex"},
        {"task_kind": "IMPLEMENTATION"},
    ):
        with pytest.raises(ToolError, match="Error executing tool agent_task_request"):
            await server.call_tool("agent_task_request", arguments | forbidden)
