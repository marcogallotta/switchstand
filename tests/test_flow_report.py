import asyncio
import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand import flow_report
from switchstand.canonical_relations import work_dependencies, work_parents
from switchstand.canonical_work import canonical_metadata, canonical_work, normalize_title
from switchstand.flow_report import report
from switchstand.work_events import work_events


@pytest.fixture
async def engine(database_prerequisite: None) -> AsyncGenerator[AsyncEngine]:
    del database_prerequisite
    url = os.getenv("TEST_DATABASE_URL")
    assert url
    database = make_url(url).set(database=f"flow_report_{uuid4().hex}")
    admin = create_async_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.exec_driver_sql(f'CREATE DATABASE "{database.database}"')
    await admin.dispose()
    subject = create_async_engine(database)
    async with subject.begin() as connection:
        await connection.run_sync(canonical_metadata.create_all)
    try:
        yield subject
    finally:
        await subject.dispose()
        admin = create_async_engine(
            make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT",
        )
        async with admin.connect() as connection:
            await connection.exec_driver_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{database.database}' AND pid <> pg_backend_pid()"
            )
            await connection.exec_driver_sql(f'DROP DATABASE "{database.database}"')
        await admin.dispose()


async def add_work(
    engine: AsyncEngine, work_id: UUID, *, root: str | None = "NONE",
    admitted_at: datetime | None = None,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(insert(canonical_work).values(
            work_id=work_id, title=str(work_id), normalized_title=normalize_title(str(work_id)),
            completed=False, notes="", canonical_root=root, admitted_at=admitted_at,
            row_version=1,
        ))


@pytest.mark.parametrize(
    ("root_value", "expected", "reason"),
    (("EXACT", "EXACT", None), ("DANGLING", "UNKNOWN", "DANGLING_WORK_ID"),
     ("invalid", "UNKNOWN", "INVALID_WORK_ID"), ("NONE", "NONE", None),
     ("UNKNOWN", "UNKNOWN", "EXPLICIT_UNKNOWN"), (None, "UNKNOWN", "NOT_CAPTURED")),
)
async def test_root_modes_are_explicit_and_report_stays_partial(
    engine: AsyncEngine, root_value: str | None, expected: str, reason: str | None,
) -> None:
    target, existing, dangling = uuid4(), uuid4(), uuid4()
    await add_work(engine, existing)
    roots = {"EXACT": str(existing), "DANGLING": str(dangling)}
    await add_work(engine, target, root=roots.get(root_value, root_value))

    result = await report(engine, target)

    assert result["status"] == "PARTIAL"
    assert result["admission"] == {"status": "UNKNOWN", "at": None,
                                   "reason": "NOT_CAPTURED"}
    assert result["scope"]["canonical_root"]["status"] == expected
    assert result["scope"]["canonical_root"]["reason"] == reason
    assert result["snapshot"] == {
        "isolation": "repeatable read", "read_only": "on", "row_version": 1,
    }
    assert result["coverage"]["timing_journal"] == {
        "status": "EXCLUDED", "reason": "RETENTION_NOT_PROVED",
    }
    assert result["coverage"]["messages"] == {
        "status": "EXCLUDED", "reason": "AMBIGUOUS_ENDPOINT_NAMESPACE",
    }
    assert result["elapsed"] == "NOT_COMPUTED_B1"


async def test_current_relations_and_target_events_are_exact_but_observed_only(
    engine: AsyncEngine,
) -> None:
    root, parent, target, dependency, unrelated = (uuid4() for _ in range(5))
    admitted = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    for item in (root, parent, dependency, unrelated):
        await add_work(engine, item)
    await add_work(engine, target, root=str(root), admitted_at=admitted)
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).where(
            canonical_work.c.work_id == target
        ).values(
            lifecycle_state="WAITING", wait_kind="DEPENDENCY",
            unblock_condition="dependency lands", next_due="UNKNOWN",
            next_action_class="NONE", next_action_ref="NONE",
        ))
        await connection.execute(insert(work_parents), [
            {"child_work_id": target, "parent_work_id": parent},
            {"child_work_id": parent, "parent_work_id": root},
        ])
        await connection.execute(insert(work_dependencies).values(
            work_id=target, depends_on_work_id=dependency,
        ))
        await connection.execute(insert(work_events), [
            {"id": uuid4(), "work_id": target, "sequence": 1, "result_version": 1,
             "subtype": "comment", "created_at": admitted},
            {"id": uuid4(), "work_id": unrelated, "sequence": 1, "result_version": 1,
             "subtype": "unrelated", "created_at": admitted},
        ])

    result = await report(engine, target)

    assert result["admission"] == {"status": "KNOWN", "at": admitted.isoformat(),
                                   "reason": None}
    assert result["current"] == {
        "completed": False, "lifecycle_state": "WAITING", "wait_kind": "DEPENDENCY",
        "unblock_condition": "dependency lands", "next_due": "UNKNOWN",
        "next_action_class": "NONE", "next_action_ref": "NONE",
        "coverage": "CURRENT_ONLY",
    }
    assert result["scope"] == {
        "canonical_root": {"status": "EXACT", "work_id": str(root), "reason": None},
        "parent_ancestry": [str(parent), str(root)],
        "parent_cycle_detected": False,
        "dependencies": [str(dependency)],
        "coverage": "CURRENT_ONLY",
    }
    assert result["events"]["coverage"] == "OBSERVED_ONLY"
    assert [item["subtype"] for item in result["events"]["items"]] == ["comment"]


async def test_parent_recursion_stops_and_reports_a_corrupt_cycle(engine: AsyncEngine) -> None:
    first, second = uuid4(), uuid4()
    await add_work(engine, first, root="UNKNOWN")
    await add_work(engine, second)
    async with engine.begin() as connection:
        await connection.execute(insert(work_parents), [
            {"child_work_id": first, "parent_work_id": second},
            {"child_work_id": second, "parent_work_id": first},
        ])

    result = await report(engine, first)

    assert result["scope"]["parent_cycle_detected"] is True
    assert result["scope"]["parent_ancestry"] == [str(second), str(first)]


async def test_repeatable_snapshot_excludes_relation_committed_mid_report(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, dependency = uuid4(), uuid4()
    await add_work(engine, target)
    await add_work(engine, dependency)
    entered, committed = asyncio.Event(), asyncio.Event()
    original = flow_report._relations

    async def paused(connection, work_id):
        entered.set()
        await committed.wait()
        return await original(connection, work_id)

    monkeypatch.setattr(flow_report, "_relations", paused)
    pending = asyncio.create_task(report(engine, target))
    await entered.wait()
    async with engine.begin() as connection:
        await connection.execute(insert(work_dependencies).values(
            work_id=target, depends_on_work_id=dependency,
        ))
    committed.set()

    result = await pending

    assert result["scope"]["dependencies"] == []
    async with engine.connect() as connection:
        assert await connection.scalar(select(work_dependencies.c.work_id)) == target


async def test_unknown_work_fails_without_fabricating_a_report(engine: AsyncEngine) -> None:
    with pytest.raises(LookupError, match="does not exist"):
        await report(engine, uuid4())
