import asyncio
import json
import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand import flow_report
from switchstand.canonical_relations import work_dependencies, work_parents
from switchstand.canonical_work import (
    canonical_metadata,
    canonical_revision,
    canonical_work,
    normalize_title,
)
from switchstand.flow_report import report
from switchstand.human_reviews import human_review_consequences
from switchstand.outcome_state import OutcomeItem, OutcomeStateStore
from switchstand.state import metadata as state_metadata
from switchstand.state import outcome_state_revisions, work_handles
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
        await connection.run_sync(state_metadata.create_all)
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
    assert result["elapsed"]["wall_status"] == "UNKNOWN"
    assert result["elapsed"]["wall_reason"] == "NOT_CAPTURED"


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
    async with engine.begin() as connection:
        await connection.execute(insert(work_handles).values(
            id=target, provider="test", provider_work_id=str(target),
        ))
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
    store = OutcomeStateStore(engine)
    await store.record(
        owner_work_id=target, active_work_id=target, operation_id=uuid4(),
        expected_state_id=None, owner_currentness_token=canonical_revision(target, 1),
        items=(OutcomeItem(
            item_key="late", description="late", kind="DELIVERABLE", status="READY",
            who_acts="OWNER",
        ),),
    )
    committed.set()

    result = await pending

    assert result["scope"]["dependencies"] == []
    assert result["outcome_state"]["status"] == "NONE"
    assert (await report(engine, target))["outcome_state"]["status"] == "KNOWN"
    async with engine.connect() as connection:
        assert await connection.scalar(select(work_dependencies.c.work_id)) == target


async def test_unknown_work_fails_without_fabricating_a_report(engine: AsyncEngine) -> None:
    with pytest.raises(LookupError, match="does not exist"):
        await report(engine, uuid4())


async def test_review_waits_are_target_only_clipped_unioned_and_observational(
    engine: AsyncEngine,
) -> None:
    target, unrelated = uuid4(), uuid4()
    admitted = datetime.now(UTC) - timedelta(seconds=30)
    await add_work(engine, target, admitted_at=admitted)
    await add_work(engine, unrelated, admitted_at=admitted)

    def review(
        work_id: UUID, created_at: datetime, decided_at: datetime | None,
    ) -> dict[str, object]:
        decision = "APPROVED" if decided_at else None
        return {
            "consequence_id": uuid4(), "package_work_id": work_id,
            "package_revision": "revision", "consequence_digest": "a" * 64,
            "consequence": {}, "decision": decision,
            "state": "READY_FOR_IMPLEMENTATION" if decision else "PENDING",
            "created_at": created_at, "decided_at": decided_at,
        }

    async with engine.begin() as connection:
        await connection.execute(insert(human_review_consequences), [
            review(target, admitted - timedelta(seconds=10),
                   admitted + timedelta(seconds=10)),
            review(target, admitted + timedelta(seconds=5),
                   admitted + timedelta(seconds=15)),
            review(target, admitted + timedelta(seconds=20), None),
            review(unrelated, admitted, admitted + timedelta(seconds=25)),
        ])
        await connection.execute(insert(work_events).values(
            id=uuid4(), work_id=target, sequence=1, result_version=1,
            subtype="point", created_at=admitted + timedelta(seconds=2),
        ))

    result = await report(engine, target)

    reviews = result["human_review"]
    assert reviews["coverage"] == "DIRECT_PACKAGE_WORK_ID"
    assert len(reviews["items"]) == 3
    assert [item["wait"]["status"] for item in reviews["items"]] == [
        "CLOSED", "CLOSED", "OPEN",
    ]
    assert all(item["wait"]["kind"] == "REVIEW" for item in reviews["items"])
    assert all(item["wait"]["clock_basis"] == "RECORDED_WALL_TIME"
               for item in reviews["items"])
    evidence = result["ordered_evidence"]
    assert evidence["meaning"] == "OBSERVATIONAL_NOT_CAUSAL"
    assert len(evidence["items"]) == 6
    assert all(item["duration_ms"] == 0 for item in evidence["items"])
    elapsed = result["elapsed"]
    assert elapsed["wall_status"] == "KNOWN"
    assert elapsed["wall_start"] == admitted.isoformat()
    assert elapsed["wall_end"] == result["captured_at"]
    assert elapsed["projection_status"] == "KNOWN"
    assert elapsed["observed_interval_union_ms"] == elapsed["wall_ms"] - 5_000
    assert elapsed["unobserved_wall_ms"] == 5_000
    assert elapsed["unobserved_interpretation"] == "NOT_IDLE_OR_CRITICAL_PATH"
    assert result["coverage"]["failures"] == {
        "status": "EXCLUDED", "reason": "NOT_INCLUDED_B4",
    }


async def test_future_admission_fails_wall_accounting_closed(engine: AsyncEngine) -> None:
    target = uuid4()
    await add_work(engine, target, admitted_at=datetime.now(UTC) + timedelta(days=1))

    result = await report(engine, target)

    assert result["elapsed"]["wall_status"] == "UNKNOWN"
    assert result["elapsed"]["wall_reason"] == "CLOCK_SKEW_OR_FUTURE_START"
    assert result["elapsed"]["observed_interval_union_ms"] is None
    assert result["elapsed"]["unobserved_wall_ms"] is None


async def test_outcome_revisions_are_exact_current_privacy_safe_and_fail_closed(
    engine: AsyncEngine,
) -> None:
    target, unrelated = uuid4(), uuid4()
    await add_work(engine, target)
    await add_work(engine, unrelated)
    async with engine.begin() as connection:
        await connection.execute(insert(work_handles), [
            {"id": target, "provider": "test", "provider_work_id": str(target)},
            {"id": unrelated, "provider": "test", "provider_work_id": str(unrelated)},
        ])
    store = OutcomeStateStore(engine)
    secret = "private description must not escape"
    ready = OutcomeItem(
        item_key="ready", description=secret, kind="DELIVERABLE", status="READY",
        who_acts="OWNER",
    )
    done = OutcomeItem(
        item_key="done", description="also private", kind="DELIVERABLE", status="DONE",
        who_acts="OWNER",
    )
    first = await store.record(
        owner_work_id=target, active_work_id=target, operation_id=uuid4(),
        expected_state_id=None, owner_currentness_token="stale-token", items=(ready, done),
    )
    assert first.state_id is not None
    current_token = canonical_revision(target, 1)
    second = await store.record(
        owner_work_id=target, active_work_id=target, operation_id=uuid4(),
        expected_state_id=first.state_id, owner_currentness_token=current_token,
        items=(ready.model_copy(update={"status": "IN_PROGRESS"}), done),
    )
    unrelated_revision = await store.record(
        owner_work_id=unrelated, active_work_id=unrelated, operation_id=uuid4(),
        expected_state_id=None, owner_currentness_token=canonical_revision(unrelated, 1),
        items=(ready,),
    )

    result = await report(engine, target)

    outcome = result["outcome_state"]
    assert outcome["status"] == "KNOWN"
    assert outcome["correlation"] == "DIRECT_OWNER_WORK_ID"
    assert outcome["total_revisions"] == 2 and outcome["truncated"] is False
    assert [item["currentness"] for item in outcome["revisions"]] == ["STALE", "CURRENT"]
    assert outcome["revisions"][0]["item_status_counts"] == {
        "NOT_STARTED": 0, "READY": 1, "IN_PROGRESS": 0, "DONE": 1,
    }
    assert unrelated_revision.state_id not in {
        UUID(item["state_id"]) for item in outcome["revisions"]
    }
    encoded = json.dumps(outcome)
    assert secret not in encoded and "also private" not in encoded
    assert "stale-token" not in encoded and current_token not in encoded
    assert all(item["duration_ms"] == 0 for item in result["ordered_evidence"]["items"]
               if item["source"] == "outcome_state")

    async with engine.begin() as connection:
        await connection.execute(update(outcome_state_revisions).where(
            outcome_state_revisions.c.state_id == second.state_id
        ).values(predecessor_id=unrelated_revision.state_id))
    corrupt_predecessor = await report(engine, target)
    assert corrupt_predecessor["outcome_state"]["status"] == "UNKNOWN"
    async with engine.begin() as connection:
        await connection.execute(update(outcome_state_revisions).where(
            outcome_state_revisions.c.state_id == second.state_id
        ).values(predecessor_id=first.state_id, content_digest="0" * 64))
    corrupt_digest = await report(engine, target)
    assert corrupt_digest["outcome_state"] == {
        "status": "UNKNOWN", "reason": "CORRUPT_REVISION_CHAIN",
        "correlation": "DIRECT_OWNER_WORK_ID", "total_revisions": None,
        "truncated": None, "revisions": [],
    }
    assert corrupt_digest["coverage"]["outcome_state"] == {
        "status": "UNKNOWN", "reason": "CORRUPT_REVISION_CHAIN",
    }
