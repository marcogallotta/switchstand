"""Private read-only current-snapshot evidence for one exact canonical WorkId."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from .activation_continuity import ActivationContract, Obligation, seal_obligation
from .canonical_relations import work_dependencies
from .canonical_work import canonical_revision, canonical_work
from .failure_journal import (
    FailureRecord,
    FailureResolution,
    failure_records,
    failure_resolutions,
    validate_failure_record_row,
    validate_failure_resolution_row,
)
from .human_reviews import human_review_consequences
from .human_trajectory import validated_trajectory_headers
from .observability import timing_persistence
from .outcome_state import validated_revision_headers
from .repository_candidate import (
    QualificationGate,
    RepositoryCandidateQualification,
    qualify_repository_candidate,
)
from .reviews import ReviewOccurrenceState
from .state import human_trajectory_revisions, outcome_state_revisions
from .task_runs import (
    task_run_executions,
    task_run_requests,
    task_run_results,
    validated_task_run_request,
    validated_task_run_result,
)
from .work_events import work_events

OUTCOME_REVISION_LIMIT = 64
TRAJECTORY_REVISION_LIMIT = 64
FAILURE_LIMIT = 128
ACTIVATION_OBLIGATION_LIMIT = 32
ACTIVATION_REVISION_LIMIT = 128
TIMING_LIMIT = 256
RUN_RECEIPT_LIMIT = 128


def _time(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


async def _run_receipts(
    connection: AsyncConnection, work_id: UUID, captured_at: datetime,
) -> tuple[dict[str, object], bool]:
    """Project exact managed-run mechanism facts without task payloads."""
    available = True
    for name in ("task_run_requests", "task_run_executions", "task_run_results"):
        available = available and bool(await connection.scalar(text(
            "SELECT to_regclass(:table) IS NOT NULL"
        ), {"table": f"public.{name}"}))
    if not available:
        return ({
            "status": "UNKNOWN", "reason": "SOURCE_TABLE_UNAVAILABLE",
            "correlation": "DIRECT_REQUESTER_OR_EXECUTION_WORK_ID",
            "total_attempts": None, "retained_attempts": 0, "truncated": None,
            "cumulative_observed_duration_ms": None, "attempts": [],
        }, False)
    correlated = or_(
        task_run_requests.c.requester_work_id == work_id,
        task_run_requests.c.execution_work_id == work_id,
    )
    total = cast(int, await connection.scalar(
        select(func.count()).select_from(task_run_requests).where(correlated)
    ))
    rows = (await connection.execute(select(task_run_requests).where(correlated).order_by(
        task_run_requests.c.created_at.desc(), task_run_requests.c.request_id.desc(),
    ).limit(RUN_RECEIPT_LIMIT))).mappings().all()
    request_ids = [cast(UUID, row["request_id"]) for row in rows]
    execution_rows = cast(list[RowMapping], (await connection.execute(
        select(task_run_executions).where(
        task_run_executions.c.request_id.in_(request_ids)
    ))).mappings().all()) if request_ids else []
    executions: dict[UUID, RowMapping] = {
        cast(UUID, row["request_id"]): row for row in execution_rows
    }
    terminal_ids = [cast(UUID, row["terminal_result_id"]) for row in rows
                    if row["terminal_result_id"] is not None]
    result_rows = cast(list[RowMapping], (await connection.execute(
        select(task_run_results).where(
        task_run_results.c.result_id.in_(terminal_ids)
    ))).mappings().all()) if terminal_ids else []
    results: dict[UUID, RowMapping] = {
        cast(UUID, row["result_id"]): row for row in result_rows
    }
    attempts: list[dict[str, object]] = []
    cumulative = 0
    for row in reversed(rows):
        try:
            request = await validated_task_run_request(connection, row)
            execution = executions.get(cast(UUID, row["request_id"]))
            terminal_id = cast(UUID | None, row["terminal_result_id"])
            result_row = None if terminal_id is None else results.get(terminal_id)
            result = (
                None if result_row is None else validated_task_run_result(result_row)
            )
        except (TypeError, ValueError):
            request, execution, result_row, result = None, None, None, None
        requested_value: object = row["created_at"]
        bound_value: object | None = None if execution is None else execution["bound_at"]
        completed_value: object | None = (
            None if result_row is None else result_row["created_at"]
        )
        requested = requested_value if isinstance(requested_value, datetime) else None
        bound = bound_value if isinstance(bound_value, datetime) else None
        completed = completed_value if isinstance(completed_value, datetime) else None
        invalid = request is None or (
            requested is None or requested.utcoffset() is None
            or requested > captured_at
            or (execution is not None and (
                bound is None or bound.utcoffset() is None
                or bound < requested or bound > captured_at
            ))
            or (result_row is not None and (
                completed is None or completed.utcoffset() is None
                or bound is None or completed < bound or completed > captured_at
            ))
            or (request.terminal_result_id is None) != (result is None)
            or (result is not None and (
                execution is None or result.run_id != cast(UUID, execution["run_id"])
                or result.request_id != request.request_id
            ))
        )
        if invalid:
            return ({
                "status": "UNKNOWN", "reason": "CORRUPT_OR_UNSAFE_RECEIPT",
                "correlation": "DIRECT_REQUESTER_OR_EXECUTION_WORK_ID",
                "total_attempts": None, "retained_attempts": 0, "truncated": None,
                "cumulative_observed_duration_ms": None, "attempts": [],
            }, True)
        assert request is not None and requested is not None
        duration = (
            int((completed - requested).total_seconds() * 1000)
            if completed is not None else None
        )
        if duration is not None:
            cumulative += duration
        roles = [
            role for role, value in (
                ("requester_work_id", request.requester_work_id),
                ("execution_work_id", request.execution_work_id),
            ) if value == work_id
        ]
        attempts.append({
            "request_id": str(request.request_id),
            "operation_id": str(cast(UUID, row["operation_id"])),
            "run_id": (None if execution is None
                       else str(cast(UUID, execution["run_id"]))),
            "task_kind": request.task_kind, "continuation": request.continuation,
            "candidate_ref": request.candidate_ref, "correlation_roles": roles,
            "state": ("COMPLETE" if completed is not None else
                      "RUNNING" if bound is not None else "REQUESTED"),
            "outcome": None if result is None else result.outcome,
            "requested_at": _time(requested),
            "bound_at": _time(bound), "completed_at": _time(completed),
            "observed_duration_ms": duration,
        })
    return ({
        "status": "PARTIAL" if total > len(attempts) else "KNOWN",
        "reason": "MOST_RECENT_ATTEMPTS_ONLY" if total > len(attempts) else None,
        "correlation": "DIRECT_REQUESTER_OR_EXECUTION_WORK_ID",
        "total_attempts": total, "retained_attempts": len(attempts),
        "truncated": total > len(attempts),
        "cumulative_observed_duration_ms": cumulative, "attempts": attempts,
    }, True)


def _review_wait(
    created_at: datetime, decided_at: datetime | None, captured_at: datetime,
) -> dict[str, object]:
    end = decided_at or captured_at
    valid = (created_at.utcoffset() is not None and end.utcoffset() is not None
             and end >= created_at)
    return {
        "kind": "REVIEW",
        "status": ("CLOSED" if decided_at is not None else "OPEN") if valid else "UNKNOWN",
        "reason": None if valid else "CLOCK_SKEW_OR_NAIVE_TIMESTAMP",
        "start": _time(created_at), "end": _time(end),
        "duration_ms": int((end - created_at).total_seconds() * 1000) if valid else None,
        "clock_basis": "RECORDED_WALL_TIME",
    }


def _outcome_projection(
    values: list[Mapping[Any, Any]], work_id: UUID, currentness_token: str,
    captured_at: datetime,
) -> dict[str, object]:
    chain = validated_revision_headers(values, currentness_token)
    if chain is None or any(row.owner_work_id != work_id for row in chain):
        return {"status": "UNKNOWN", "reason": "CORRUPT_REVISION_CHAIN",
                "correlation": "DIRECT_OWNER_WORK_ID", "total_revisions": None,
                "truncated": None, "revisions": []}
    if not chain:
        return {"status": "NONE", "reason": "NO_REVISIONS",
                "correlation": "DIRECT_OWNER_WORK_ID", "total_revisions": 0,
                "truncated": False, "revisions": []}
    if any(not isinstance(value.get("created_at"), datetime)
           or cast(datetime, value["created_at"]).utcoffset() is None for value in values):
        return {"status": "UNKNOWN", "reason": "UNSAFE_CREATED_AT",
                "correlation": "DIRECT_OWNER_WORK_ID", "total_revisions": None,
                "truncated": None, "revisions": []}
    if any(cast(datetime, value["created_at"]) > captured_at for value in values):
        return {"status": "UNKNOWN", "reason": "CLOCK_SKEW_OR_FUTURE_CREATED_AT",
                "correlation": "DIRECT_OWNER_WORK_ID", "total_revisions": None,
                "truncated": None, "revisions": []}
    selected = list(zip(values, chain, strict=True))[-OUTCOME_REVISION_LIMIT:]
    revisions: list[dict[str, object]] = []
    for value, row in selected:
        revisions.append({
            "state_id": str(row.state_id), "generation": row.generation,
            "schema_version": value["schema_version"],
            "created_at": _time(cast(datetime, value["created_at"])),
            "currentness": row.currentness,
            "item_status_counts": dict(row.item_status_counts),
        })
    return {
        "status": "KNOWN", "reason": None, "correlation": "DIRECT_OWNER_WORK_ID",
        "total_revisions": len(chain), "truncated": len(chain) > len(revisions),
        "revisions": revisions,
    }


def _trajectory_projection(
    values: list[tuple[object, ...]], work_id: UUID, captured_at: datetime,
) -> dict[str, object]:
    chain = validated_trajectory_headers(values)
    if chain is None or any(row.work_id != work_id for row in chain):
        return {"status": "UNKNOWN", "reason": "CORRUPT_OR_MISMATCHED_CHAIN",
                "chain_status": "UNKNOWN", "correlation": "DIRECT_WORK_ID_REF",
                "total_revisions": None, "truncated": None, "revisions": []}
    if any(row.created_at.utcoffset() is None for row in chain):
        return {"status": "UNKNOWN", "reason": "UNSAFE_CREATED_AT",
                "chain_status": "UNKNOWN", "correlation": "DIRECT_WORK_ID_REF",
                "total_revisions": None, "truncated": None, "revisions": []}
    if any(row.created_at > captured_at for row in chain):
        return {"status": "UNKNOWN", "reason": "CLOCK_SKEW_OR_FUTURE_CREATED_AT",
                "chain_status": "UNKNOWN", "correlation": "DIRECT_WORK_ID_REF",
                "total_revisions": None, "truncated": None, "revisions": []}
    selected = chain[-TRAJECTORY_REVISION_LIMIT:]
    return {
        "status": "KNOWN" if chain else "NONE",
        "reason": None if chain else "NO_REVISIONS",
        "chain_status": "VALIDATED", "correlation": "DIRECT_WORK_ID_REF",
        "total_revisions": len(chain), "truncated": len(chain) > len(selected),
        "revisions": [{
            "trajectory_id": str(row.trajectory_id), "generation": row.generation,
            "source_kind": row.source_kind.value, "created_at": _time(row.created_at),
        } for row in selected],
    }


async def _relations(
    connection: AsyncConnection, work_id: UUID,
) -> tuple[list[str], list[str], bool]:
    ancestry = (await connection.execute(text("""
        WITH RECURSIVE ancestry(work_id, depth, path, cycle) AS (
          SELECT parent_work_id, 1, ARRAY[child_work_id, parent_work_id], false
          FROM work_parents WHERE child_work_id = CAST(:work_id AS uuid)
          UNION ALL
          SELECT parent.parent_work_id, ancestry.depth + 1,
                 ancestry.path || parent.parent_work_id,
                 parent.parent_work_id = ANY(ancestry.path)
          FROM work_parents parent JOIN ancestry ON parent.child_work_id = ancestry.work_id
          WHERE NOT ancestry.cycle
        )
        SELECT work_id, cycle FROM ancestry ORDER BY depth
    """), {"work_id": str(work_id)})).all()
    dependencies = (await connection.scalars(select(
        work_dependencies.c.depends_on_work_id
    ).where(work_dependencies.c.work_id == work_id).order_by(
        work_dependencies.c.depends_on_work_id
    ))).all()
    return (
        [str(row[0]) for row in ancestry],
        [str(value) for value in dependencies],
        any(cast(bool, row[1]) for row in ancestry),
    )


async def _human_reviews(
    connection: AsyncConnection, work_id: UUID,
) -> tuple[list[Any], bool]:
    available = await connection.scalar(
        text("SELECT to_regclass('public.human_review_consequences') IS NOT NULL")
    )
    if not available:
        return [], False
    rows = (await connection.execute(select(
        human_review_consequences.c.consequence_id,
        human_review_consequences.c.state,
        human_review_consequences.c.decision,
        human_review_consequences.c.created_at,
        human_review_consequences.c.decided_at,
    ).where(human_review_consequences.c.package_work_id == work_id).order_by(
        human_review_consequences.c.created_at,
        human_review_consequences.c.consequence_id,
    ))).all()
    return list(rows), True


async def _failures(
    connection: AsyncConnection, work_id: UUID, captured_at: datetime,
) -> tuple[dict[str, object], bool]:
    available = cast(bool, await connection.scalar(text(
        "SELECT to_regclass('public.failure_records') IS NOT NULL "
        "AND to_regclass('public.failure_resolutions') IS NOT NULL"
    )))
    if not available:
        return {"status": "UNKNOWN", "reason": "SOURCE_TABLE_UNAVAILABLE", "items": []}, False
    record_fields = [
        failure_records.c[name].label(name) for name in FailureRecord.model_fields
    ]
    resolution_fields = [
        failure_resolutions.c[name].label(f"resolution_{name}")
        for name in FailureResolution.model_fields
    ]
    total = cast(int, await connection.scalar(select(func.count()).select_from(
        failure_records
    ).where(failure_records.c.owner == str(work_id))))
    rows = (await connection.execute(select(
        *record_fields, failure_records.c.content_digest,
        *resolution_fields,
        failure_resolutions.c.content_digest.label("resolution_content_digest"),
    ).select_from(failure_records.outerjoin(
        failure_resolutions,
        failure_resolutions.c.attempt_id == failure_records.c.attempt_id,
    )).where(failure_records.c.owner == str(work_id)).order_by(
        failure_records.c.occurred_at.desc(), failure_records.c.attempt_id.desc(),
    ).limit(FAILURE_LIMIT))).mappings().all()
    items: list[dict[str, object]] = []
    for row in rows:
        stored = {str(key): value for key, value in row.items()}
        record = validate_failure_record_row(stored)
        resolution = (
            None if stored["resolution_resolution_id"] is None
            else validate_failure_resolution_row(stored)
        )
        if record is None or (
            stored["resolution_resolution_id"] is not None and resolution is None
        ):
            return {"status": "UNKNOWN", "reason": "CORRUPT_RECORD", "items": []}, True
        if record.occurred_at > captured_at or (
            resolution is not None and (
                resolution.resolved_at > captured_at
                or resolution.resolved_at < record.occurred_at
            )
        ):
            return {"status": "UNKNOWN", "reason": "UNSAFE_TIMESTAMP", "items": []}, True
        items.append({
            "attempt_id": str(record.attempt_id),
            "operation_id": str(record.operation_id),
            "effect_state": record.effect_state.value,
            "occurred_at": _time(record.occurred_at),
            "resolution_id": None if resolution is None else str(resolution.resolution_id),
            "resolved_at": None if resolution is None else _time(resolution.resolved_at),
            "resolution_latency_ms": None if resolution is None else int(
                (resolution.resolved_at - record.occurred_at).total_seconds() * 1000
            ),
        })
    items.reverse()
    truncated = total > len(items)
    return {
        "status": "PARTIAL" if truncated else "KNOWN",
        "reason": "MOST_RECENT_RECORDS_ONLY" if truncated else None,
        "correlation": "DIRECT_OWNER_WORK_ID", "items": items,
        "total_records": total, "truncated": truncated,
        "open_count": (None if truncated else sum(
            item["resolved_at"] is None for item in items
        )),
        "observed_open_count": sum(item["resolved_at"] is None for item in items),
    }, True


async def _activation(
    connection: AsyncConnection, work_id: UUID,
) -> tuple[dict[str, object], bool]:
    available = cast(bool, await connection.scalar(text(
        "SELECT to_regclass('public.activation_obligation_revisions') IS NOT NULL"
    )))
    if not available:
        return {"status": "UNKNOWN", "reason": "SOURCE_TABLE_UNAVAILABLE", "items": []}, False
    obligation_ids = list((await connection.scalars(text(
        "SELECT DISTINCT obligation_id FROM activation_obligation_revisions "
        "WHERE record->'binding'->>'product_work_id'=:work_id "
        "OR record->'binding'->>'return_owner_work_id'=:work_id "
        "ORDER BY obligation_id LIMIT :limit"
    ), {"work_id": str(work_id), "limit": ACTIVATION_OBLIGATION_LIMIT + 1})).all())
    obligations_truncated = len(obligation_ids) > ACTIVATION_OBLIGATION_LIMIT
    selected_ids = obligation_ids[:ACTIVATION_OBLIGATION_LIMIT]
    if not selected_ids:
        return {
            "status": "KNOWN", "reason": None, "correlation": "DIRECT_CONTRACT_WORK_ID",
            "items": [], "total_obligations": 0, "truncated": False,
        }, True
    counts = (await connection.execute(text(
        "SELECT obligation_id,count(*) AS revision_count "
        "FROM activation_obligation_revisions WHERE obligation_id=ANY(:ids) "
        "GROUP BY obligation_id"
    ), {"ids": selected_ids})).mappings().all()
    if any(cast(int, row["revision_count"]) > ACTIVATION_REVISION_LIMIT for row in counts):
        return {
            "status": "UNKNOWN", "reason": "REVISION_LIMIT_EXCEEDED", "items": [],
        }, True
    rows = (await connection.execute(text(
        "SELECT obligation_id,operation_id,generation,record "
        "FROM activation_obligation_revisions WHERE obligation_id=ANY(:ids) "
        "ORDER BY obligation_id,generation"
    ), {"ids": selected_ids})).mappings().all()
    grouped: dict[UUID, list[Mapping[Any, Any]]] = {}
    for row in rows:
        grouped.setdefault(cast(UUID, row["obligation_id"]), []).append(row)
    items: list[dict[str, object]] = []
    for obligation_id, chain_rows in grouped.items():
        previous: str | None = None
        chain: list[Obligation] = []
        binding: dict[str, object] | None = None
        try:
            for expected_generation, row in enumerate(chain_rows, 1):
                value = Obligation.model_validate(row["record"])
                if (
                    row["obligation_id"] != obligation_id
                    or row["generation"] != expected_generation
                    or value.generation != expected_generation
                    or value.predecessor != previous
                    or value.operation_id != row["operation_id"]
                    or seal_obligation(value).digest != value.digest
                    or (binding is not None and value.binding != binding)
                ):
                    raise ValueError("invalid activation revision chain")
                previous = value.digest
                binding = value.binding
                chain.append(value)
        except (KeyError, TypeError, ValueError):
            return {"status": "UNKNOWN", "reason": "CORRUPT_REVISION_CHAIN", "items": []}, True
        latest = chain[-1]
        raw_binding = dict(latest.binding)
        embedded_obligation_id = raw_binding.pop("obligation_id", None)
        try:
            contract = ActivationContract.model_validate(raw_binding)
        except (TypeError, ValueError):
            return {"status": "UNKNOWN", "reason": "INVALID_CONTRACT_BINDING", "items": []}, True
        if str(contract.obligation_id) != str(embedded_obligation_id) or (
            contract.obligation_id != obligation_id
        ):
            return {"status": "UNKNOWN", "reason": "OBLIGATION_ID_MISMATCH", "items": []}, True
        binding = contract.model_dump(mode="json")
        roles = [name for name in ("product_work_id", "return_owner_work_id")
                 if binding.get(name) == str(work_id)]
        items.append({
            "obligation_id": str(obligation_id), "generation": latest.generation,
            "state": latest.state, "acceptance": latest.acceptance,
            "adoption": latest.adoption, "target_phase": binding.get("target_phase"),
            "target_revision": binding.get("target_revision"), "correlation_roles": roles,
        })
    return {
        "status": "PARTIAL",
        "reason": ("OBLIGATION_ID_LIMIT_EXCEEDED" if obligations_truncated
                   else "SOURCE_HAS_NO_REVISION_TIMESTAMPS"),
        "correlation": "DIRECT_CONTRACT_WORK_ID", "items": items,
        "total_obligations": (
            f">={ACTIVATION_OBLIGATION_LIMIT + 1}" if obligations_truncated else len(items)
        ),
        "truncated": obligations_truncated,
    }, True


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3)


async def _mcp_timings(
    connection: AsyncConnection, work_id: UUID, write_failed: bool,
) -> tuple[dict[str, object], bool]:
    available = cast(bool, await connection.scalar(text(
        "SELECT to_regclass('public.mcp_operation_timings') IS NOT NULL"
    )))
    if not available:
        return {"status": "UNKNOWN", "reason": "SOURCE_TABLE_UNAVAILABLE"}, False
    total = cast(int, await connection.scalar(text(
        "SELECT count(*) FROM mcp_operation_timings WHERE target_work_id=:work_id"
    ), {"work_id": work_id}))
    rows = (await connection.execute(text(
        "SELECT tool,duration_ms,db_total_ms,db_max_ms,child_union_ms,"
        "server_residual_ms,runtime_generation,schema_generation "
        "FROM mcp_operation_timings WHERE target_work_id=:work_id "
        "ORDER BY started_at DESC,call_id DESC LIMIT :limit"
    ), {"work_id": work_id, "limit": TIMING_LIMIT})).mappings().all()
    unstable = any(
        row["runtime_generation"] is None or row["schema_generation"] == "UNKNOWN"
        for row in rows
    )
    groups: dict[tuple[str, str, str], list[float]] = {}
    for row in rows:
        runtime = row["runtime_generation"]
        key = (str(runtime), str(row["schema_generation"]), str(row["tool"]))
        if runtime is not None and "UNKNOWN" not in key:
            groups.setdefault(key, []).append(float(row["duration_ms"]))
    truncated = total > len(rows)
    reasons = ["BEST_EFFORT_NONRETRYING_CAPTURE", *[
        reason for condition, reason in (
            (write_failed, "PERSISTENCE_FAILURE_IN_PROCESS"),
            (truncated, "MOST_RECENT_RECORDS_ONLY"),
            (unstable, "UNSTABLE_COHORT_EXCLUDED"),
        ) if condition
    ]]
    return {
        "status": "PARTIAL", "reason": "+".join(reasons),
        "correlation": "DIRECT_TARGET_WORK_ID",
        "interpretation": "SERVICE_TIME_NOT_CRITICAL_PATH",
        "total_records": total, "retained_records": len(rows), "truncated": truncated,
        "service_total_ms": round(sum(float(row["duration_ms"]) for row in rows), 3),
        "db_total_ms": round(sum(float(row["db_total_ms"]) for row in rows), 3),
        "db_max_ms": round(max((float(row["db_max_ms"]) for row in rows), default=0), 3),
        "child_union_ms": round(sum(float(row["child_union_ms"]) for row in rows), 3),
        "server_residual_ms": round(sum(float(row["server_residual_ms"]) for row in rows), 3),
        "cohorts": [{
            "runtime_generation": runtime, "schema_generation": schema, "tool": tool,
            "sample_count": len(durations), "p50_ms": _percentile(durations, .5),
            "p90_ms": _percentile(durations, .9),
        } for (runtime, schema, tool), durations in sorted(groups.items())],
    }, True


async def _snapshot(
    connection: AsyncConnection, work_id: UUID,
    review_occurrences: ReviewOccurrenceState | None = None,
    timing_write_failed: bool = False,
) -> dict[str, object]:
    await connection.execute(text("SET TRANSACTION READ ONLY"))
    captured_at = cast(datetime, await connection.scalar(select(func.now())))
    isolation = cast(str, await connection.scalar(text("SHOW transaction_isolation")))
    read_only = cast(str, await connection.scalar(text("SHOW transaction_read_only")))
    work = (await connection.execute(select(
        canonical_work.c.row_version, canonical_work.c.admitted_at,
        canonical_work.c.canonical_root, canonical_work.c.completed,
        canonical_work.c.lifecycle_state, canonical_work.c.wait_kind,
        canonical_work.c.unblock_condition, canonical_work.c.next_due,
        canonical_work.c.next_action_class, canonical_work.c.next_action_ref,
    ).where(canonical_work.c.work_id == work_id))).one_or_none()
    if work is None:
        raise LookupError("canonical work does not exist")
    ancestors, dependencies, cycle = await _relations(connection, work_id)
    events = (await connection.execute(select(
        work_events.c.id, work_events.c.sequence, work_events.c.subtype,
        work_events.c.result_version, work_events.c.created_at,
    ).where(work_events.c.work_id == work_id).order_by(work_events.c.sequence))).all()
    reviews, reviews_available = await _human_reviews(connection, work_id)
    outcome_values = (await connection.execute(select(outcome_state_revisions).where(
        outcome_state_revisions.c.owner_work_id == work_id
    ).order_by(outcome_state_revisions.c.generation))).mappings().all()
    trajectory_values = (await connection.execute(select(human_trajectory_revisions).where(
        human_trajectory_revisions.c.work_id_ref == work_id
    ).order_by(human_trajectory_revisions.c.generation))).all()
    failures, _failures_available = await _failures(connection, work_id, captured_at)
    activation, activation_available = await _activation(connection, work_id)
    mcp_timings, timings_available = await _mcp_timings(connection, work_id, timing_write_failed)
    run_receipts, run_receipts_available = await _run_receipts(
        connection, work_id, captured_at,
    )

    raw_root = cast(str | None, work.canonical_root)
    root_id: UUID | None = None
    if raw_root not in {None, "NONE", "UNKNOWN"}:
        try:
            parsed = UUID(raw_root)
            root_id = parsed if str(parsed) == raw_root else None
        except ValueError:
            pass
    root_exists = root_id is not None and await connection.scalar(select(
        canonical_work.c.work_id
    ).where(canonical_work.c.work_id == root_id)) is not None
    if raw_root is None:
        root_status, root_reason = "UNKNOWN", "NOT_CAPTURED"
    elif raw_root == "NONE":
        root_status, root_reason = "NONE", None
    elif raw_root == "UNKNOWN":
        root_status, root_reason = "UNKNOWN", "EXPLICIT_UNKNOWN"
    elif root_id is None:
        root_status, root_reason = "UNKNOWN", "INVALID_WORK_ID"
    elif not root_exists:
        root_status, root_reason = "UNKNOWN", "DANGLING_WORK_ID"
    else:
        root_status, root_reason = "EXACT", None
    admission = cast(datetime | None, work.admitted_at)
    outcome = _outcome_projection(
        list(outcome_values), work_id,
        canonical_revision(work_id, cast(int, work.row_version)),
        captured_at,
    )
    trajectory = _trajectory_projection(
        [tuple(value) for value in trajectory_values], work_id, captured_at,
    )
    review_pickup = (
        {"status": "UNKNOWN", "reason": "SOURCE_UNAVAILABLE", "phase": None,
         "unpicked": None,
         "oldest_request_age_ms": None, "requested_at": None,
         "received_at": None, "verdict_at": None}
        if review_occurrences is None else await review_occurrences.pickup_projection(
            connection, work_id,
            canonical_revision(work_id, cast(int, work.row_version)), captured_at,
        )
    )
    review_items = [{
        "consequence_id": str(row.consequence_id),
        "state": row.state, "decision": row.decision,
        "prepared_at": _time(row.created_at), "decided_at": _time(row.decided_at),
        "wait": _review_wait(row.created_at, row.decided_at, captured_at),
    } for row in reviews]
    ordered_evidence = [{
        "source": "work_events", "kind": row.subtype, "id": str(row.id),
        "at": _time(row.created_at), "duration_ms": 0,
    } for row in events]
    for row in reviews:
        ordered_evidence.append({
            "source": "human_review", "kind": "PREPARED",
            "id": str(row.consequence_id), "at": _time(row.created_at), "duration_ms": 0,
        })
        if row.decided_at is not None:
            ordered_evidence.append({
                "source": "human_review", "kind": "DECIDED",
                "id": str(row.consequence_id), "at": _time(row.decided_at), "duration_ms": 0,
            })
    for revision in cast(list[dict[str, object]], outcome["revisions"]):
        ordered_evidence.append({
            "source": "outcome_state", "kind": "REVISION",
            "id": revision["state_id"], "at": revision["created_at"], "duration_ms": 0,
        })
    for revision in cast(list[dict[str, object]], trajectory["revisions"]):
        ordered_evidence.append({
            "source": "human_trajectory", "kind": "REVISION",
            "id": revision["trajectory_id"], "at": revision["created_at"], "duration_ms": 0,
        })
    for item in cast(list[dict[str, object]], failures["items"]):
        ordered_evidence.append({
            "source": "failure_journal", "kind": "FAILURE_RECORDED",
            "id": item["attempt_id"], "at": item["occurred_at"], "duration_ms": 0,
        })
        if item["resolved_at"] is not None:
            ordered_evidence.append({
                "source": "failure_journal", "kind": "FAILURE_RESOLVED",
                "id": item["resolution_id"], "at": item["resolved_at"], "duration_ms": 0,
            })
    for attempt in cast(list[dict[str, object]], run_receipts["attempts"]):
        ordered_evidence.append({
            "source": "task_runs", "kind": "REQUESTED",
            "id": attempt["request_id"], "at": attempt["requested_at"], "duration_ms": 0,
        })
        if attempt["bound_at"] is not None:
            ordered_evidence.append({
                "source": "task_runs", "kind": "BOUND",
                "id": attempt["request_id"], "at": attempt["bound_at"], "duration_ms": 0,
            })
        if attempt["completed_at"] is not None:
            ordered_evidence.append({
                "source": "task_runs", "kind": "COMPLETED",
                "id": attempt["request_id"], "at": attempt["completed_at"], "duration_ms": 0,
            })
    ordered_evidence.sort(key=lambda item: (cast(str, item["at"]),
                                            cast(str, item["source"]),
                                            cast(str, item["id"])))
    value: dict[str, object] = {
        "schema": "switchstand.flow_report.v1", "status": "PARTIAL",
        "work_id": str(work_id), "captured_at": _time(captured_at),
        "snapshot": {"isolation": isolation, "read_only": read_only,
                     "row_version": work.row_version},
        "admission": {"status": "KNOWN" if admission else "UNKNOWN",
                      "at": _time(admission),
                      "reason": None if admission else "NOT_CAPTURED"},
        "current": {
            "completed": work.completed, "lifecycle_state": work.lifecycle_state,
            "wait_kind": work.wait_kind, "unblock_condition": work.unblock_condition,
            "next_due": work.next_due, "next_action_class": work.next_action_class,
            "next_action_ref": work.next_action_ref, "coverage": "CURRENT_ONLY",
        },
        "scope": {
            "canonical_root": {"status": root_status,
                               "work_id": str(root_id) if root_id is not None else None,
                               "reason": root_reason},
            "parent_ancestry": ancestors, "parent_cycle_detected": cycle,
            "dependencies": dependencies, "coverage": "CURRENT_ONLY",
        },
        "events": {
            "coverage": "OBSERVED_ONLY",
            "items": [{
                "id": str(row.id), "sequence": row.sequence,
                "subtype": row.subtype, "result_version": row.result_version,
                "at": _time(row.created_at),
            } for row in events],
        },
        "human_review": {
            "coverage": "DIRECT_PACKAGE_WORK_ID" if reviews_available else "UNKNOWN",
            "reason": None if reviews_available else "SOURCE_TABLE_UNAVAILABLE",
            "items": review_items,
        },
        "outcome_state": outcome,
        "human_trajectory": trajectory,
        "failures": failures,
        "activation": activation,
        "mcp_tracker_timing": mcp_timings,
        "run_receipts": run_receipts,
        "review_pickup": review_pickup,
        "ordered_evidence": {
            "meaning": "OBSERVATIONAL_NOT_CAUSAL",
            "items": ordered_evidence,
        },
        "coverage": {
            "canonical_work": {"status": "INCLUDED", "reason": "CURRENT_ONLY"},
            "relations": {"status": "INCLUDED", "reason": "CURRENT_ONLY"},
            "work_events": {"status": "INCLUDED", "reason": "OBSERVED_ONLY"},
            "human_review": {
                "status": "INCLUDED" if reviews_available else "UNKNOWN",
                "reason": "DIRECT_PACKAGE_WORK_ID" if reviews_available
                else "SOURCE_TABLE_UNAVAILABLE",
            },
            "outcome_state": {
                "status": "UNKNOWN" if outcome["status"] == "UNKNOWN" else "INCLUDED",
                "reason": outcome["reason"] if outcome["status"] == "UNKNOWN"
                else "DIRECT_OWNER_WORK_ID",
            },
            "human_trajectory": {
                "status": "UNKNOWN" if trajectory["status"] == "UNKNOWN" else "INCLUDED",
                "reason": trajectory["reason"] if trajectory["status"] == "UNKNOWN"
                else "DIRECT_WORK_ID_REF",
            },
            "messages": {"status": "EXCLUDED", "reason": "AMBIGUOUS_ENDPOINT_NAMESPACE"},
            "review_pickup": {
                "status": review_pickup["status"], "reason": review_pickup["reason"],
            },
            "timing_journal": {
                "status": ("UNKNOWN" if not timings_available else
                           "PARTIAL" if mcp_timings["status"] == "PARTIAL" else "INCLUDED"),
                "reason": mcp_timings["reason"],
            },
            "failures": {
                "status": "INCLUDED" if failures["status"] == "KNOWN" else "UNKNOWN",
                "reason": "DIRECT_OWNER_WORK_ID" if failures["status"] == "KNOWN"
                else failures["reason"],
            },
            "activation": {
                "status": ("INCLUDED" if activation_available
                           and activation["status"] != "UNKNOWN" else "UNKNOWN"),
                "reason": activation["reason"],
            },
            "run_receipts": {
                "status": ("INCLUDED" if run_receipts_available
                           and run_receipts["status"] != "UNKNOWN" else "UNKNOWN"),
                "reason": run_receipts["reason"],
            },
            **{name: {"status": "EXCLUDED", "reason": "NOT_INCLUDED_V2_SLICE_A"}
               for name in ("lifecycle_timing", "github")},
        },
        "source_coverage": {
            "canonical_work": {"status": "PARTIAL", "reason": "CURRENT_ONLY"},
            "review": {"status": "KNOWN" if reviews_available else "UNKNOWN",
                       "reason": None if reviews_available else "SOURCE_TABLE_UNAVAILABLE"},
            "review_pickup": {
                "status": "KNOWN" if review_pickup["status"] != "UNKNOWN" else "UNKNOWN",
                "reason": review_pickup["reason"],
            },
            "github_ci": {"status": "UNKNOWN", "reason": "EXACT_CANDIDATE_NOT_CORRELATED"},
            "test_metrics": {"status": "UNKNOWN", "reason": "EXACT_RUN_NOT_CORRELATED"},
            "failure_recovery": {"status": failures["status"], "reason": failures["reason"]},
            "activation": {"status": activation["status"], "reason": activation["reason"]},
            "mcp_tracker_timing": {
                "status": mcp_timings["status"], "reason": mcp_timings["reason"],
            },
            "delivery_recovery_attempts": {
                "status": run_receipts["status"], "reason": run_receipts["reason"],
            },
        },
    }
    return project_wall(value)


async def report(
    engine: AsyncEngine, work_id: UUID,
    review_occurrences: ReviewOccurrenceState | None = None,
) -> dict[str, object]:
    store = timing_persistence(engine)
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="REPEATABLE READ")
        async with connection.begin():
            return await _snapshot(connection, work_id, review_occurrences, store is not None and store.write_failed)


def _gate_interval(gate: QualificationGate) -> tuple[datetime, datetime] | None:
    try:
        started = datetime.fromisoformat(cast(str, gate.started_at))
        completed = datetime.fromisoformat(cast(str, gate.completed_at))
    except (TypeError, ValueError):
        return None
    if started.utcoffset() is None or completed.utcoffset() is None or completed < started:
        return None
    return started, completed


def _union_ms(intervals: list[tuple[datetime, datetime]]) -> int:
    total = 0
    ordered = sorted(intervals)
    current_start, current_end = ordered[0]
    for started, completed in ordered[1:]:
        if started <= current_end:
            current_end = max(current_end, completed)
        else:
            total += int((current_end - current_start).total_seconds() * 1000)
            current_start, current_end = started, completed
    return total + int((current_end - current_start).total_seconds() * 1000)


def _aware(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _unknown_elapsed(reason: str, start: object = None, end: object = None) -> dict[str, object]:
    return {
        "clock_basis": "RECORDED_WALL_TIME",
        "wall_status": "UNKNOWN", "wall_reason": reason,
        "wall_start": start, "wall_end": end, "wall_ms": None,
        "projection_status": "UNKNOWN", "projection_reason": reason,
        "observed_interval_union_ms": None, "unobserved_wall_ms": None,
        "unobserved_interpretation": "NOT_IDLE_OR_CRITICAL_PATH",
    }


def project_wall(value: dict[str, object]) -> dict[str, object]:
    """Project only validated recorded-wall intervals into the admission window."""
    result = dict(value)
    admission = cast(dict[str, object], value["admission"])
    start, end = _aware(admission.get("at")), _aware(value.get("captured_at"))
    if admission.get("status") != "KNOWN" or start is None or end is None:
        result["elapsed"] = _unknown_elapsed(
            "NOT_CAPTURED", admission.get("at"), value.get("captured_at"),
        )
        return result
    if start > end:
        result["elapsed"] = _unknown_elapsed(
            "CLOCK_SKEW_OR_FUTURE_START", admission.get("at"), value.get("captured_at"),
        )
        return result

    raw_intervals: list[tuple[object, object]] = []
    reviews = cast(dict[str, object], value.get("human_review", {}))
    for item in cast(list[dict[str, object]], reviews.get("items", [])):
        wait = cast(dict[str, object], item["wait"])
        raw_intervals.append(
            (wait.get("start"), wait.get("end"))
            if wait.get("status") in {"OPEN", "CLOSED"} else (None, None)
        )
    github = cast(dict[str, object] | None, value.get("github"))
    if github is not None and github.get("status") == "OBSERVED":
        subjects = cast(dict[str, dict[str, object]], github["subjects"])
        for subject in subjects.values():
            for interval in cast(list[dict[str, object]], subject["intervals"]):
                raw_intervals.append((interval.get("started_at"), interval.get("completed_at")))
    failures = cast(dict[str, object], value.get("failures", {}))
    if failures.get("status") in {"KNOWN", "PARTIAL"}:
        for item in cast(list[dict[str, object]], failures.get("items", [])):
            if item.get("resolved_at") is not None:
                raw_intervals.append((item.get("occurred_at"), item.get("resolved_at")))
    run_receipts = cast(dict[str, object], value.get("run_receipts", {}))
    if run_receipts.get("status") in {"KNOWN", "PARTIAL"}:
        for attempt in cast(list[dict[str, object]], run_receipts.get("attempts", [])):
            if attempt.get("completed_at") is not None:
                raw_intervals.append((attempt.get("requested_at"), attempt.get("completed_at")))

    clipped: list[tuple[datetime, datetime]] = []
    for raw_start, raw_end in raw_intervals:
        interval_start, interval_end = _aware(raw_start), _aware(raw_end)
        if interval_start is None or interval_end is None or interval_end < interval_start:
            result["elapsed"] = {
                "clock_basis": "RECORDED_WALL_TIME",
                "wall_status": "KNOWN", "wall_reason": None,
                "wall_start": admission["at"], "wall_end": value["captured_at"],
                "wall_ms": int((end - start).total_seconds() * 1000),
                "projection_status": "UNKNOWN", "projection_reason": "UNSAFE_INTERVAL",
                "observed_interval_union_ms": None, "unobserved_wall_ms": None,
                "unobserved_interpretation": "NOT_IDLE_OR_CRITICAL_PATH",
            }
            return result
        clipped_start, clipped_end = max(start, interval_start), min(end, interval_end)
        if clipped_start < clipped_end:
            clipped.append((clipped_start, clipped_end))
    wall_ms = int((end - start).total_seconds() * 1000)
    union_ms = _union_ms(clipped) if clipped else 0
    result["elapsed"] = {
        "clock_basis": "RECORDED_WALL_TIME",
        "wall_status": "KNOWN", "wall_reason": None,
        "wall_start": admission["at"], "wall_end": value["captured_at"],
        "wall_ms": wall_ms,
        "projection_status": "KNOWN", "projection_reason": None,
        "observed_interval_union_ms": union_ms,
        "unobserved_wall_ms": wall_ms - union_ms,
        "unobserved_interpretation": "NOT_IDLE_OR_CRITICAL_PATH",
    }
    return result


def _github_subject(
    qualification: RepositoryCandidateQualification,
    kind: Literal["exact_head", "composition"],
    expected_sha: str | None,
) -> dict[str, object]:
    gates = [gate for gate in qualification.gates if gate.subject_kind == kind]
    valid_reasons = {None, "failed", "cancelled", "skipped"}
    parsed = [_gate_interval(gate) for gate in gates]
    if (not expected_sha or not gates or any(gate.subject_sha != expected_sha for gate in gates)
            or any(gate.reason not in valid_reasons for gate in gates)):
        return {"status": "UNKNOWN", "reason": "GATE_IDENTITY_UNKNOWN",
                "subject_sha": expected_sha, "intervals": [], "union_ms": None}
    if any(interval is None for interval in parsed):
        return {"status": "UNKNOWN", "reason": "INCOMPLETE_GATE_TIMESTAMPS",
                "subject_sha": expected_sha, "intervals": [], "union_ms": None}
    intervals = cast(list[tuple[datetime, datetime]], parsed)
    return {
        "status": "OBSERVED", "reason": None, "subject_sha": expected_sha,
        "intervals": [{
            "gate": gate.name, "state": gate.state, "conclusion": gate.conclusion,
            "started_at": gate.started_at, "completed_at": gate.completed_at,
        } for gate in gates],
        "union_ms": _union_ms(intervals),
    }


def add_github_evidence(
    value: dict[str, object], qualification: RepositoryCandidateQualification,
    expected_head_sha: str,
) -> dict[str, object]:
    """Add only exact, caller-correlated GitHub timing evidence."""
    result = dict(value)
    reason = None
    if qualification.status == "UNKNOWN":
        reason = "PROVIDER_UNAVAILABLE"
    elif qualification.head_sha != expected_head_sha:
        reason = "EXPECTED_HEAD_MISMATCH"
    elif qualification.reason in {"candidate_changed", "composition_mismatch"}:
        reason = "STALE_OR_MISMATCHED_CANDIDATE"
    subjects: dict[str, dict[str, object]]
    if reason:
        subjects = {
            kind: {"status": "UNKNOWN", "reason": reason, "subject_sha": None,
                   "intervals": [], "union_ms": None}
            for kind in ("exact_head", "composition")
        }
    else:
        subjects = {
            "exact_head": _github_subject(qualification, "exact_head", expected_head_sha),
            "composition": _github_subject(
                qualification, "composition", qualification.composition_sha,
            ),
        }
        reason = next((cast(str, item["reason"]) for item in subjects.values()
                       if item["status"] == "UNKNOWN"), None)
    status = "UNKNOWN" if reason else "OBSERVED"
    result["github"] = {
        "status": status, "reason": reason, "correlation": "CALLER_SUPPLIED",
        "pull_request": qualification.pull_request,
        "expected_head_sha": expected_head_sha,
        "observed_head_sha": qualification.head_sha,
        "qualification_status": qualification.status,
        "subjects": subjects,
    }
    coverage = dict(cast(dict[str, object], value["coverage"]))
    coverage["github"] = {
        "status": "INCLUDED" if status == "OBSERVED" else "UNKNOWN",
        "reason": "CALLER_SUPPLIED" if status == "OBSERVED" else reason,
    }
    result["coverage"] = coverage
    source_coverage = dict(cast(dict[str, object], value.get("source_coverage", {})))
    source_coverage["github_ci"] = {
        "status": "KNOWN" if status == "OBSERVED" else "UNKNOWN",
        "reason": "CALLER_SUPPLIED" if status == "OBSERVED" else reason,
    }
    result["source_coverage"] = source_coverage
    return project_wall(result) if "admission" in result else result


def render_concise(value: dict[str, object]) -> str:
    """Render only explicit B1 facts and coverage, without deriving new evidence."""
    snapshot = cast(dict[str, object], value["snapshot"])
    admission = cast(dict[str, object], value["admission"])
    current = cast(dict[str, object], value["current"])
    scope = cast(dict[str, object], value["scope"])
    root = cast(dict[str, object], scope["canonical_root"])
    events = cast(dict[str, object], value["events"])
    coverage = cast(dict[str, dict[str, str]], value["coverage"])
    lines = [
        f"status={value['status']} work_id={value['work_id']} captured_at={value['captured_at']}",
        (f"snapshot row_version={snapshot['row_version']} isolation={snapshot['isolation']} "
         f"read_only={snapshot['read_only']}"),
        (f"admission status={admission['status']} at={admission['at']} "
         f"reason={admission['reason']}"),
        (f"current completed={current['completed']} lifecycle_state={current['lifecycle_state']} "
         f"wait_kind={current['wait_kind']} unblock_condition={current['unblock_condition']} "
         f"next_due={current['next_due']} next_action_class={current['next_action_class']} "
         f"next_action_ref={current['next_action_ref']} coverage={current['coverage']}"),
        f"root status={root['status']} work_id={root['work_id']} reason={root['reason']}",
        (f"relations parent_cycle_detected={scope['parent_cycle_detected']} "
         f"warning={'CORRUPT_CYCLE' if scope['parent_cycle_detected'] else None}"),
        (f"evidence_count={len(cast(list[object], events['items']))} "
         f"events_coverage={events['coverage']}"),
    ]
    human_review = cast(dict[str, object] | None, value.get("human_review"))
    if human_review is not None:
        review_items = sorted(
            cast(list[dict[str, object]], human_review["items"]),
            key=lambda item: (cast(str, item["prepared_at"]),
                              cast(str, item["consequence_id"])),
        )
        lines.append(
            f"human_review_count={len(review_items)} "
            f"coverage={human_review['coverage']} reason={human_review.get('reason')}"
        )
        for item in review_items:
            identifier = item["consequence_id"]
            lines.append(
                f"human_review_record id={identifier} state={item['state']} "
                f"decision={item['decision']}"
            )
            lines.append(
                f"human_review_point id={identifier} kind=PREPARED "
                f"at={item['prepared_at']} duration_ms=0"
            )
            if item["decided_at"] is not None:
                lines.append(
                    f"human_review_point id={identifier} kind=DECIDED "
                    f"at={item['decided_at']} duration_ms=0"
                )
            wait = cast(dict[str, object], item["wait"])
            lines.append(
                f"human_review_wait id={identifier} kind={wait['kind']} "
                f"status={wait['status']} reason={wait['reason']} start={wait['start']} "
                f"end={wait['end']} duration_ms={wait['duration_ms']} "
                f"clock_basis={wait['clock_basis']}"
            )
    github = cast(dict[str, object] | None, value.get("github"))
    if github is not None:
        lines.append(
            f"github status={github['status']} reason={github['reason']} "
            f"correlation={github['correlation']} pull_request={github['pull_request']} "
            f"expected_head_sha={github['expected_head_sha']} "
            f"observed_head_sha={github['observed_head_sha']}"
        )
        subjects = cast(dict[str, dict[str, object]], github["subjects"])
        lines.extend(
            f"github_{kind} status={subject['status']} reason={subject['reason']} "
            f"subject_sha={subject['subject_sha']} intervals="
            f"{len(cast(list[object], subject['intervals']))} union_ms={subject['union_ms']}"
            for kind, subject in sorted(subjects.items())
        )
    outcome = cast(dict[str, object] | None, value.get("outcome_state"))
    if outcome is not None:
        lines.append(
            f"outcome_state status={outcome['status']} reason={outcome['reason']} "
            f"correlation={outcome['correlation']} total_revisions={outcome['total_revisions']} "
            f"truncated={outcome['truncated']}"
        )
        for revision in cast(list[dict[str, object]], outcome["revisions"]):
            counts = cast(dict[str, int], revision["item_status_counts"])
            lines.append(
                f"outcome_revision generation={revision['generation']} "
                f"state_id={revision['state_id']} created_at={revision['created_at']} "
                f"currentness={revision['currentness']} schema_version={revision['schema_version']} "
                f"counts=NOT_STARTED:{counts['NOT_STARTED']},READY:{counts['READY']},"
                f"IN_PROGRESS:{counts['IN_PROGRESS']},DONE:{counts['DONE']}"
            )
    trajectory = cast(dict[str, object] | None, value.get("human_trajectory"))
    if trajectory is not None:
        lines.append(
            f"human_trajectory status={trajectory['status']} reason={trajectory['reason']} "
            f"chain_status={trajectory['chain_status']} correlation={trajectory['correlation']} "
            f"total_revisions={trajectory['total_revisions']} truncated={trajectory['truncated']}"
        )
        lines.extend(
            f"trajectory_revision generation={revision['generation']} "
            f"trajectory_id={revision['trajectory_id']} source_kind={revision['source_kind']} "
            f"created_at={revision['created_at']}"
            for revision in cast(list[dict[str, object]], trajectory["revisions"])
        )
    timings = cast(dict[str, object] | None, value.get("mcp_tracker_timing"))
    if timings is not None:
        lines.append(
            f"mcp_tracker_timing status={timings['status']} reason={timings['reason']} "
            f"correlation={timings.get('correlation')} total_records={timings.get('total_records')} "
            f"retained_records={timings.get('retained_records')} truncated={timings.get('truncated')} "
            f"service_total_ms={timings.get('service_total_ms')} "
            f"db_total_ms={timings.get('db_total_ms')} "
            f"server_residual_ms={timings.get('server_residual_ms')}"
        )
        lines.extend(
            f"mcp_timing_cohort runtime={item['runtime_generation']} "
            f"schema={item['schema_generation']} tool={item['tool']} "
            f"samples={item['sample_count']} p50_ms={item['p50_ms']} p90_ms={item['p90_ms']}"
            for item in cast(list[dict[str, object]], timings.get("cohorts", []))
        )
    run_receipts = cast(dict[str, object] | None, value.get("run_receipts"))
    if run_receipts is not None:
        lines.append(
            f"run_receipts status={run_receipts['status']} reason={run_receipts['reason']} "
            f"correlation={run_receipts['correlation']} "
            f"total_attempts={run_receipts['total_attempts']} "
            f"retained_attempts={run_receipts['retained_attempts']} "
            f"truncated={run_receipts['truncated']} "
            f"cumulative_observed_duration_ms="
            f"{run_receipts['cumulative_observed_duration_ms']}"
        )
        lines.extend(
            f"run_receipt request_id={item['request_id']} run_id={item['run_id']} "
            f"task_kind={item['task_kind']} state={item['state']} outcome={item['outcome']} "
            f"candidate_ref={item['candidate_ref']} requested_at={item['requested_at']} "
            f"bound_at={item['bound_at']} completed_at={item['completed_at']} "
            f"observed_duration_ms={item['observed_duration_ms']}"
            for item in cast(list[dict[str, object]], run_receipts["attempts"])
        )
    lines.extend(
        f"coverage {name}={item['status']}:{item['reason']}"
        for name, item in sorted(coverage.items())
    )
    elapsed = cast(dict[str, object], value["elapsed"])
    lines.append(
        f"elapsed wall_status={elapsed['wall_status']} wall_reason={elapsed['wall_reason']} "
        f"wall_ms={elapsed['wall_ms']} projection_status={elapsed['projection_status']} "
        f"projection_reason={elapsed['projection_reason']} "
        f"observed_interval_union_ms={elapsed['observed_interval_union_ms']} "
        f"unobserved_wall_ms={elapsed['unobserved_wall_ms']} "
        f"interpretation={elapsed['unobserved_interpretation']}"
    )
    return "\n".join(lines) + "\n"


def render(value: dict[str, object], output_format: Literal["json", "concise"]) -> str:
    if output_format == "concise":
        return render_concise(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"


async def _run(
    work_id: UUID, output_format: Literal["json", "concise"],
    github_pr: int | None = None, github_head_sha: str | None = None,
) -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        value = await report(engine, work_id)
        if github_pr is not None and github_head_sha is not None:
            value = add_github_evidence(
                value, await qualify_repository_candidate(github_pr), github_head_sha,
            )
        print(render(value, output_format), end="")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Report exact read-only flow evidence")
    parser.add_argument("--work-id", type=UUID, required=True)
    parser.add_argument("--format", choices=("json", "concise"), default="json")
    parser.add_argument("--github-pr", type=int)
    parser.add_argument("--github-head-sha")
    arguments = parser.parse_args()
    if (arguments.github_pr is None) != (arguments.github_head_sha is None):
        parser.error("--github-pr and --github-head-sha must be supplied together")
    if arguments.github_pr is not None and arguments.github_pr < 1:
        parser.error("--github-pr must be positive")
    if arguments.github_head_sha is not None and (
        len(arguments.github_head_sha) != 40
        or any(character not in "0123456789abcdef" for character in arguments.github_head_sha)
    ):
        parser.error("--github-head-sha must be a lowercase 40-character SHA")
    asyncio.run(_run(
        arguments.work_id, arguments.format, arguments.github_pr, arguments.github_head_sha,
    ))
