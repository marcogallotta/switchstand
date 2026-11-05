"""Render exact flow evidence without deriving new facts."""

from __future__ import annotations

import json
from typing import Literal, cast


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
    aggregate = cast(dict[str, object] | None, value.get("aggregate_learning"))
    if aggregate is not None:
        cohort = cast(dict[str, object], aggregate["cohort"])
        metrics = cast(dict[str, object], aggregate["metrics"])
        lines.append(
            f"aggregate_learning status={aggregate['status']} reason={aggregate['reason']} "
            f"sample_count={aggregate['sample_count']} "
            f"coverage_fraction={aggregate['coverage_fraction']} "
            f"runtime={cohort['runtime_generation']} schema={cohort['schema_generation']} "
            f"environment={cohort['environment']} delivery_estimate="
            f"{aggregate['delivery_estimate']} metrics="
            + ",".join(f"{name}:{value}" for name, value in sorted(metrics.items()))
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
    timing = cast(dict[str, object] | None, value.get("delivery_timing"))
    if timing is not None:
        lines.append(
            f"delivery_timing status={timing['status']} reason={timing['reason']} "
            f"window={elapsed['wall_start']}..{elapsed['wall_end']} "
            f"interval_cost_sum_ms={timing['interval_cost_sum_ms']} "
            f"coverage={timing['coverage']} interpretation={timing['interpretation']}"
        )
        for item in cast(list[dict[str, object]], timing["intervals"]):
            lines.append(
                f"delivery_interval source={item['source']} source_id={item['source_id']} "
                f"kind={item['kind']} start={item['start']} end={item['end']} "
                f"duration_ms={item['duration_ms']} end_basis={item['end_basis']}"
            )
        for item in cast(list[dict[str, object]], timing["gaps"] or []):
            lines.append(
                f"unexplained_gap start={item['start']} end={item['end']} "
                f"duration_ms={item['duration_ms']} "
                f"large={cast(int, item['duration_ms']) >= 1_800_000} diagnostic_filter_ms=1800000"
            )
        for name in ("largest_interval", "largest_gap"):
            lines.append(f"delivery_{name}={json.dumps(timing[name], sort_keys=True)}")
    return "\n".join(lines) + "\n"


def render(value: dict[str, object], output_format: Literal["json", "concise"]) -> str:
    if output_format == "concise":
        return render_concise(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"


