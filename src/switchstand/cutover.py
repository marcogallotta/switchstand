"""Machine-checkable cutover coverage gate.

The required catalogue is production-owned and immutable in code. The manifest
records only current status/evidence. GAP or UNKNOWN always blocks activation.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

Status = Literal["COVERED", "NOT_REQUIRED", "GAP", "UNKNOWN"]
ALLOWED = frozenset({"COVERED", "NOT_REQUIRED", "GAP", "UNKNOWN"})
IMPLEMENTATION_OWNER = "1218606173542889"
REQUIRED_CATALOG: Mapping[str, Mapping[str, str]] = MappingProxyType({
    "connector_operations": MappingProxyType({
        "search_tasks": "provider-neutral search/filter/discovery",
        "get_tasks": "area/workflow list and pagination",
        "get_my_tasks": "assignee/My Tasks semantics if used",
        "search_objects": "task/area/actor discovery without raw provider IDs",
        "get_task": "complete current work record and relations",
        "create_tasks": "production-safe ordinary work/review/subtask creation",
        "update_tasks": "ordinary semantic fields, placement and relations",
        "add_comment": "durable history plus addressed agent communication replacement",
        "get_attachments": "attachment/evidence pointer listing and read",
        "get_project": "provider-neutral area/workflow context",
        "get_projects": "dynamic area/project discovery if used",
        "get_user": "owner/assignee identity resolution if used",
        "get_users": "owner/assignee search if used",
        "get_me": "authenticated actor identity outcome",
        "get_teams": "team discovery if current placement depends on it",
        "create_project": "ordinary project creation if any live workflow uses it",
        "create_project_status_update": "project status objects if live workflow uses them",
        "get_status_overview": "equivalent ordinary status discovery",
        "get_portfolio": "portfolio reads if live workflow uses them",
        "get_portfolios": "portfolio discovery if live workflow uses it",
        "get_items_for_portfolio": "portfolio item reads if live workflow uses them",
        "delete_task": "task deletion if any ordinary workflow requires it",
        "get_workspace_agents": "Asana AI teammate lookup if current routing uses it",
        "get_agent": "Asana AI teammate detail if current routing uses it",
        "create_task_preview_v4": "safe preview outcome if required before create",
        "search_tasks_preview": "search outcome; rendering helper need not be cloned",
        "create_project_preview_v3": "project-create preview only if project creation is required",
    }),
    "semantic_fields": MappingProxyType({
        "stable_work_id": "same WorkId throughout ordinary lifecycle",
        "title": "title/name read and write",
        "notes": "current semantic meaning read and write",
        "complete_reopen": "complete then reopen same WorkId",
        "workflow_home_state": "one authoritative workflow home and state",
        "related_membership": "RELATED secondary membership without second workflow home",
        "parent_child": "parent/child relation preserving identity",
        "dependencies": "dependencies and dependents",
        "review_relation": "review linkage",
        "root_group": "provider-neutral root/group identity",
        "priority": "P-CRITICAL/P0/P1/P2/UNSET",
        "work_type": "Work Type",
        "marco_review_next_action": "Marco review next action",
        "owner_assignee": "owner/assignee where current workflow uses it",
        "history_event": "durable append and exact provider-neutral event reread",
        "area_workflow_facts": "provider-neutral workflow interpretation facts",
        "earliest_delivery_horizon": "legacy value remains readable",
        "stage3_gate": "legacy value remains readable; no new mutation by default",
        "due_start": "due/start scheduling if any live consumer remains",
        "followers": "followers if any live consumer remains",
        "approval_objects": "Asana approval status/subtype if any live consumer remains",
        "my_tasks_placement": "assignee section/My Tasks placement if any live consumer remains",
        "tags": "tags if any live consumer remains",
        "extra_custom_fields": "arbitrary extra custom fields if any live consumer remains",
    }),
    "non_tool_dependencies": MappingProxyType({
        "workspace_authority": "workspace-scoped ChatGPT authority with explicit target revalidation",
        "agent_messaging": "message request/result/receive/disposition/replacement without Asana comment transport",
        "reliable_result_saving": "stable result duty survives restart/replacement/ambiguous provider send",
        "review_watch_routes": "reviews/watches/results use Switchstand routes with receipt/disposition semantics",
        "legacy_task_story_refs": "old Asana task/story references remain resolvable during transition",
        "late_arrivals": "late legacy comments/messages/reviews reopen reconciliation",
        "unresolved_effect_failback": "failback preserves same unresolved identities and prevents replay",
        "mcp_only_real_chatgpt": "real ChatGPT completes representative ordinary workflow with Asana connector unavailable",
    }),
})


UNCONDITIONAL: Mapping[str, frozenset[str]] = MappingProxyType({
    "connector_operations": frozenset({
        "search_tasks", "get_tasks", "search_objects", "get_task", "create_tasks",
        "update_tasks", "add_comment", "get_attachments", "get_project",
    }),
    "semantic_fields": frozenset({
        "stable_work_id", "title", "notes", "complete_reopen", "workflow_home_state",
        "related_membership", "parent_child", "dependencies", "review_relation",
        "root_group", "priority", "work_type", "marco_review_next_action",
        "owner_assignee", "history_event", "area_workflow_facts",
    }),
    "non_tool_dependencies": frozenset({
        "workspace_authority", "agent_messaging", "reliable_result_saving",
        "review_watch_routes", "legacy_task_story_refs", "late_arrivals",
        "unresolved_effect_failback", "mcp_only_real_chatgpt",
    }),
})


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _string_map(value: object, label: str) -> dict[str, str]:
    raw = _object(value, label)
    result: dict[str, str] = {}
    for key, item in raw.items():
        if not key or not isinstance(item, str):
            raise TypeError(f"{label} must map non-empty strings to strings")
        result[key] = item
    return result


def _evidence_map(value: object, label: str) -> dict[str, dict[str, str]]:
    raw = _object(value, label)
    result: dict[str, dict[str, str]] = {}
    for key, item in raw.items():
        result[key] = _string_map(item, f"{label}.{key}")
    return result


def entries(manifest: Mapping[str, object]) -> tuple[tuple[str, str, Status, dict[str, str]], ...]:
    if manifest.get("version") != 1:
        raise ValueError("unsupported cutover manifest version")
    if manifest.get("implementation_owner") != IMPLEMENTATION_OWNER:
        raise ValueError("cutover manifest implementation_owner mismatch")
    statuses = _object(manifest.get("status"), "status")
    evidence = _object(manifest.get("evidence"), "evidence")
    if set(statuses) != set(REQUIRED_CATALOG) or set(evidence) != set(REQUIRED_CATALOG):
        raise ValueError("cutover manifest groups do not match required catalogue")

    rows: list[tuple[str, str, Status, dict[str, str]]] = []
    for group, required in REQUIRED_CATALOG.items():
        actual = _string_map(statuses[group], f"status.{group}")
        proofs = _evidence_map(evidence[group], f"evidence.{group}")
        if set(actual) != set(required):
            missing = sorted(set(required) - set(actual))
            extra = sorted(set(actual) - set(required))
            raise ValueError(f"{group} identities mismatch missing={missing} extra={extra}")
        if not set(proofs) <= set(required):
            extra = sorted(set(proofs) - set(required))
            raise ValueError(f"{group} evidence has unknown identities: {extra}")
        for identity in required:
            status = actual[identity]
            if status not in ALLOWED:
                raise ValueError(f"{group}:{identity} has invalid status")
            typed_status = cast(Status, status)
            proof = proofs.get(identity, {})
            if typed_status == "NOT_REQUIRED" and identity in UNCONDITIONAL[group]:
                raise ValueError(f"{group}:{identity} is unconditional and must be COVERED")
            if typed_status == "NOT_REQUIRED":
                for field in ("current_use_evidence", "evidence"):
                    if not proof.get(field, "").strip():
                        raise ValueError(f"{group}:{identity} NOT_REQUIRED lacks {field}")
            if typed_status == "COVERED":
                for field in (
                    "current_use_evidence", "mcp_surface", "positive_test", "negative_test", "evidence"
                ):
                    if not proof.get(field, "").strip():
                        raise ValueError(f"{group}:{identity} COVERED lacks {field}")
            rows.append((group, identity, typed_status, proof))
    return tuple(rows)


def blockers(manifest: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(
        f"{group}:{identity}:{status}"
        for group, identity, status, _proof in entries(manifest)
        if status in {"GAP", "UNKNOWN"}
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load(path: Path) -> Mapping[str, object]:
    raw: object = json.loads(path.read_text(), object_pairs_hook=_unique_object)
    value = _object(raw, "cutover manifest")
    entries(value)
    return value


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m switchstand.cutover <coverage-manifest.json>")
    try:
        pending = blockers(load(Path(sys.argv[1])))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "INVALID", "error": str(error)}, sort_keys=True))
        raise SystemExit(2) from error
    if pending:
        print(json.dumps({"status": "BLOCKED", "blockers": pending}, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps({"status": "READY"}, sort_keys=True))


if __name__ == "__main__":
    main()
