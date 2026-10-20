from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from chatgpt_fixture import PRINCIPAL
from mcp import Client
from pydantic import ValidationError
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.agent_mailboxes import AgentMailbox, AgentMailboxState, agent_mailboxes
from switchstand.agent_status import AgentStatusState, StatusWrite, project
from switchstand.canonical_relations import (
    CanonicalRelationsRepository,
    project_memberships,
    projects,
)
from switchstand.canonical_work import CanonicalWorkRepository, canonical_work
from switchstand.canonical_work_runtime import CanonicalWorkRuntime
from switchstand.chatgpt import ChatGPTService
from switchstand.chatgpt_mcp import build_ordinary_tools
from switchstand.contracts import WorkSearchRequest
from switchstand.grant_state import GrantState
from switchstand.messages import MessageState
from switchstand.reviews import ReviewResult
from switchstand.state import PostgresState


@pytest.fixture
async def status_owner(canonical_database_schema):
    engine = create_async_engine(canonical_database_schema)
    async with engine.begin() as connection:
        await connection.exec_driver_sql("TRUNCATE agent_mailboxes CASCADE")
        await connection.exec_driver_sql("TRUNCATE canonical_work, projects CASCADE")
    mailboxes = AgentMailboxState(engine)
    alpha = (await mailboxes.register_agent("Alpha", PRINCIPAL.key, "a")).mailbox
    beta = (await mailboxes.register_agent("Beta", PRINCIPAL.key, "b")).mailbox
    assert alpha is not None and beta is not None
    yield engine, mailboxes, alpha, beta
    await engine.dispose()


async def test_real_cross_project_pagination_and_task_preimage_preserved(status_owner):
    engine, _mailboxes, alpha, _beta = status_owner
    project_ids = [uuid4(), uuid4()]
    records = [{"work_id": uuid4(), "title": f"Task {i:02}", "normalized_title": f"task {i:02}",
        "notes": "current evidence", "row_version": 1, "completed": False, "owner_key": "agent:alpha"} for i in range(52)]
    async with engine.begin() as connection:
        await connection.execute(insert(projects), [{"project_id": p, "name": n} for p, n in zip(project_ids, ("Alpha", "Beta"), strict=True)])
        await connection.execute(insert(canonical_work), records)
        await connection.execute(insert(project_memberships), [{"project_id": project_ids[i % 2], "work_id": r["work_id"]} for i, r in enumerate(records)])
        before = (await connection.execute(select(canonical_work).order_by(canonical_work.c.work_id))).all()
    runtime = CanonicalWorkRuntime(CanonicalWorkRepository(engine), CanonicalRelationsRepository(engine))
    first = await runtime.search(WorkSearchRequest(api_version="1", owner_key="agent:alpha", completed=False, limit=50))
    assert len(first.items) == 50 and first.next_cursor
    second = await runtime.search(WorkSearchRequest(api_version="1", owner_key="agent:alpha", completed=False, cursor=first.next_cursor, limit=50))
    assert len(second.items) == 2 and second.next_cursor is None
    assert {p.area for item in (*first.items, *second.items) for p in item.context.placements} == {"Alpha", "Beta"}
    state = AgentStatusState(engine)
    await state.write(alpha, StatusWrite(operation_id=uuid4(), expected_generation=0,
        report="First page only; remaining work not reconciled", coverage="PARTIAL"))
    assert (await state.get(alpha, alpha)).document["coverage"] == "PARTIAL"
    async with engine.connect() as connection:
        after = (await connection.execute(select(canonical_work).order_by(canonical_work.c.work_id))).all()
    assert after == before


async def test_persisted_report_replay_cas_and_replacement(status_owner):
    engine, mailboxes, alpha, beta = status_owner
    state = AgentStatusState(engine)
    assert (await state.get(alpha, alpha)).status == "MISSING"
    request = StatusWrite(operation_id=uuid4(), expected_generation=0,
                         report="Alpha/A awaits R; Beta/B current and next; C captured only", coverage="PARTIAL")
    first = await state.write(alpha, request)
    assert first.status == "ok" and not first.due
    assert await state.write(alpha, request) == first
    changed = request.model_copy(update={"report": "changed"})
    assert (await state.write(alpha, changed)).status == "CONFLICT"
    fresh = create_async_engine(engine.url)
    async with fresh.connect() as connection:
        row = (await connection.execute(select(agent_mailboxes).where(
            agent_mailboxes.c.name_key == "alpha",
        ))).mappings().one()
        assert row["agent_status"]["report"] == request.report
        assert row["generation"] == alpha.generation and row["session_key"] == alpha.session_key
    await fresh.dispose()
    assert (await state.get(alpha, beta)).status == "MISSING"
    deferred = await state.write(alpha, StatusWrite(operation_id=uuid4(), expected_generation=1,
        defer_reason="Finish current incident mitigation", recheck="Read list after rollback readback", defer_seconds=900))
    assert deferred.document["reported_at"] == first.document["reported_at"]
    assert not deferred.due and deferred.generation == 2
    assert (await state.write(alpha, request)).status == "STALE"
    moved = (await mailboxes.takeover("Alpha", PRINCIPAL.key, "replacement")).mailbox
    assert moved is not None
    assert (await state.write(alpha, request)).status == "STALE"
    assert (await state.get(moved, moved)).status == "STALE_BINDING"
    assert (await state.get(moved, moved)).due


def test_due_boundaries_and_deferral_expiry(monkeypatch):
    alpha = AgentMailbox(name="Alpha", name_key="alpha", endpoint_id=uuid4(),
                         principal_key=PRINCIPAL.key, session_key="a", generation=1)
    now = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr("switchstand.agent_status.datetime", Clock)
    value = {"report": "No fabricated progress; coverage gap C", "generation": 1,
             "report_binding": 1, "reported_at": (now - timedelta(seconds=899)).isoformat()}
    assert not project(alpha, value).due
    value["reported_at"] = (now - timedelta(seconds=900)).isoformat()
    assert project(alpha, value).due
    value.update(defer_binding=1, defer_until=(now + timedelta(seconds=1)).isoformat())
    assert not project(alpha, value).due
    value["defer_until"] = now.isoformat()
    assert project(alpha, value).due
    with pytest.raises(ValidationError):
        StatusWrite(operation_id=uuid4(), expected_generation=0, defer_reason=" ", recheck="soon", defer_seconds=900)
    with pytest.raises(ValidationError):
        StatusWrite(operation_id=uuid4(), expected_generation=0, report="done", coverage="UNKNOWN", defer_seconds=1)


async def test_real_mcp_status_tools_admission_and_primary_result(status_owner, monkeypatch):
    engine, _mailboxes, _alpha, _beta = status_owner
    grants = GrantState(engine)
    service = ChatGPTService(AsyncMock(return_value=PRINCIPAL), PostgresState(engine), grants, {}, MessageState(engine, grants),
                            ordinary_workspace_admission=True, reviews=AsyncMock())
    tools = dict(build_ordinary_tools(service, agent_identity=lambda: "a"))
    missing = await tools["agent_status_get"]("1")
    assert missing.status == "MISSING" and missing.owner_key == "agent:alpha"
    # Public MCP registration/schema and actual tool request validation, with the real PG owner.
    from mcp.server import MCPServer

    from switchstand.chatgpt_mcp import closed_tool, ordinary_tool_annotations
    server = MCPServer("status test")
    for name, function in tools.items():
        closed_tool(server, name, function, ordinary_tool_annotations(name))
    async with Client(server) as client:
        result = await client.call_tool("agent_status_update", {"api_version": "1", "operation_id": str(uuid4()),
            "expected_generation": 0, "report": "A waiting; B next; C capture only", "coverage": "UNKNOWN"})
        assert not result.is_error and result.structured_content["generation"] == 1
        assert (await client.call_tool("agent_status_get", {"api_version": "1", "name": "Beta"})).structured_content["name"] == "Beta"
    baseline = await service.get(uuid4())
    got = await tools["work_get"]("1", uuid4())
    assert got.status == baseline.status and got.agent_status_checkpoint is None
    async def broken(*args):
        raise ValueError("unavailable report")
    monkeypatch.setattr(AgentStatusState, "get", broken)
    failed_hint = await tools["work_get"]("1", uuid4())
    assert failed_hint.status == baseline.status and "UNKNOWN" in failed_hint.agent_status_checkpoint
    for review_status in ("SENT", "WAITING_REVIEWER", "PASS"):
        primary = ReviewResult(status=review_status, review_id=uuid4(),
            delivery_id=None if review_status == "WAITING_REVIEWER" else uuid4(),
            reason="reviewer_not_available" if review_status == "WAITING_REVIEWER" else None,
            verdict="PASS" if review_status == "PASS" else None,
            verdict_digest="receipt" if review_status == "PASS" else None)
        request = AsyncMock(return_value=primary)
        monkeypatch.setattr(service, "reviews", SimpleNamespace(request=request))
        with monkeypatch.context() as fault:
            fault.setattr(service, "admission_get", AsyncMock(side_effect=RuntimeError("advisory unavailable")))
            receipt = await tools["review_request"]("1", uuid4(), "revision", "CODE")
        request.assert_awaited_once()
        assert "UNKNOWN" in receipt.agent_status_checkpoint
        assert receipt.model_dump(exclude={"next_action", "agent_status_checkpoint"}) == primary.model_dump(exclude={"next_action"})
        if review_status == "WAITING_REVIEWER":
            assert receipt.next_action.endswith(primary.next_action)
            assert "due task-list check" in receipt.next_action
        elif review_status == "SENT":
            assert "bounded sleep 20" in receipt.next_action
            assert "already-authorized runnable work" in receipt.next_action
        else:
            assert receipt.next_action == primary.next_action
    service.admission_grants = grants
    assert (await tools["agent_status_get"]("1")).status == "DENIED"
    assert (await tools["agent_status_update"]("1", uuid4(), 1, report="unauthorized", coverage="UNKNOWN")).status == "DENIED"
