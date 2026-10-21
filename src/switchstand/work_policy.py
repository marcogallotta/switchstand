"""Resultant-state policy for canonical work semantic writes."""

from uuid import UUID

from .agent_mailboxes import CODEX_COORDINATOR_NAME

SEMANTIC_FIELDS = frozenset({
    "completed", "priority", "work_type", "lifecycle_state", "canonical_root", "owner_key",
    "wait_kind", "unblock_condition", "next_due", "next_action_class", "next_action_ref",
})

_SENTINELS = frozenset({"NONE", "UNKNOWN"})


def _exact(value: str | None) -> bool:
    return bool(value) and value not in _SENTINELS


def _root(value: str | None) -> bool:
    if value in _SENTINELS:
        return True
    if value is None:
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return value == str(parsed)


def validate_resultant_state(
    *, lifecycle_state: str | None, canonical_root: str | None,
    owner_key: str | None, wait_kind: str | None, unblock_condition: str | None,
    next_due: str | None, next_action_class: str | None, next_action_ref: str | None,
    completed: bool | None = None, affected: set[str] | frozenset[str] = SEMANTIC_FIELDS,
) -> None:
    """Reject incomplete or incoherent state after a semantic write."""
    transition = bool(affected & {"completed", "lifecycle_state"})
    waits = transition or bool(affected & {"wait_kind", "unblock_condition", "next_due"})
    actions = waits or bool(affected & {"next_action_class", "next_action_ref"})
    if (waits or actions) and lifecycle_state not in {
        "CURRENT", "WAITING", "DEFERRED", "TERMINAL", "UNKNOWN",
    }:
        raise ValueError("semantic writes require a lifecycle state")
    if "canonical_root" in affected and not _root(canonical_root):
        raise ValueError("canonical root must be NONE, UNKNOWN, or a WorkId")
    if "owner_key" in affected and not owner_key:
        raise ValueError("owner key must be NONE, UNKNOWN, or nonempty")

    if transition and completed is not None and completed != (lifecycle_state == "TERMINAL"):
        raise ValueError(
            "completed/lifecycle_state must agree: use completed=true alone to finish, "
            "or completed=false with a valid nonterminal transition"
        )

    action = (next_action_class, next_action_ref)
    if actions and action not in {("NONE", "NONE"), ("UNKNOWN", "UNKNOWN")} and not all(
        _exact(value) for value in action
    ):
        raise ValueError("next_action_class/next_action_ref require two exact values, NONE/NONE, or UNKNOWN/UNKNOWN")

    if (actions and lifecycle_state in {"WAITING", "DEFERRED", "TERMINAL"}
            and action != ("NONE", "NONE")):
        raise ValueError(
            "next_action_class/next_action_ref must both be NONE for waiting/deferred/terminal work"
        )
    if not waits:
        return
    if lifecycle_state == "CURRENT":
        if (wait_kind, unblock_condition, next_due) != ("NONE", "NONE", "NONE"):
            raise ValueError("CURRENT requires wait_kind=unblock_condition=next_due=NONE")
    elif lifecycle_state in {"WAITING", "DEFERRED"}:
        if not _exact(wait_kind) or not _exact(unblock_condition):
            raise ValueError("WAITING/DEFERRED requires exact wait_kind and unblock_condition")
        if action != ("NONE", "NONE"):
            raise ValueError("waiting/deferred work cannot carry a next action")
        if not next_due:
            raise ValueError("WAITING/DEFERRED requires an explicit next_due disposition")
    elif lifecycle_state == "TERMINAL":
        if (wait_kind, unblock_condition, next_due) != ("NONE", "NONE", "NONE"):
            raise ValueError("TERMINAL requires wait_kind=unblock_condition=next_due=NONE")
        if action != ("NONE", "NONE"):
            raise ValueError("terminal work cannot carry a next action")
    elif not all((wait_kind, unblock_condition, next_due)):
        raise ValueError("UNKNOWN requires explicit wait_kind/unblock_condition/next_due dispositions")


def validate_create_state(
    *, parented: bool, work_type: str | None, lifecycle_state: str | None,
    canonical_root: str | None, owner_key: str | None, wait_kind: str | None,
    unblock_condition: str | None, next_due: str | None,
    next_action_class: str | None, next_action_ref: str | None,
) -> None:
    """Apply the strict Step-0 contract only to newly created canonical work."""
    validate_resultant_state(
        lifecycle_state=lifecycle_state, canonical_root=canonical_root,
        owner_key=owner_key, wait_kind=wait_kind,
        unblock_condition=unblock_condition, next_due=next_due,
        next_action_class=next_action_class, next_action_ref=next_action_ref,
    )
    if canonical_root in {None, "NONE", "UNKNOWN"}:
        raise ValueError("created work requires an exact root")

    if (
        work_type, lifecycle_state, owner_key, wait_kind, unblock_condition,
        next_due, next_action_class, next_action_ref,
    ) == (
        "Task", "WAITING", "NONE", "TRIAGE", "ASSIGNMENT_REQUIRED",
        "UNSCHEDULED", "NONE", "NONE",
    ):
        return

    report = not parented and work_type in {
        "BUG", "PRODUCT_FEATURE", "PROCESS", "PERFORMANCE", "HUMAN_REVIEW_FEEDBACK",
        "FORK_MANAGEMENT",
    } and owner_key == "NONE"
    if report:
        if (
            lifecycle_state != "WAITING"
            or (wait_kind, unblock_condition, next_due)
            != ("TRIAGE", "TRIAGE_PICKUP", "UNSCHEDULED")
            or (next_action_class, next_action_ref) != ("NONE", "NONE")
        ):
            raise ValueError("ownerless reports must wait for triage")
        return

    evidence = parented and work_type == "Evidence" and owner_key == "NONE"
    if evidence:
        if (
            lifecycle_state != "CURRENT"
            or (wait_kind, unblock_condition, next_due) != ("NONE", "NONE", "NONE")
            or (next_action_class, next_action_ref) != ("NONE", "NONE")
        ):
            raise ValueError("ownerless Evidence must be an inert CURRENT child")
        return

    if work_type in {None, "UNKNOWN"}:
        raise ValueError("substantive create requires an explicit work type")
    if lifecycle_state not in {"CURRENT", "WAITING", "DEFERRED"}:
        raise ValueError("substantive create requires an actionable lifecycle")
    if (
        owner_key is None
        or not _exact(owner_key)
        or (owner_key != CODEX_COORDINATOR_NAME and not owner_key.startswith("agent:"))
    ):
        raise ValueError("substantive create requires derived agent ownership")
    if lifecycle_state == "CURRENT":
        if (wait_kind, unblock_condition, next_due) != ("NONE", "NONE", "NONE"):
            raise ValueError("current create cannot carry a wait")
        if not all(_exact(value) for value in (next_action_class, next_action_ref)):
            raise ValueError("current create requires an exact next action")
    elif (
        (next_action_class, next_action_ref) != ("NONE", "NONE")
        or not all(_exact(value) for value in (wait_kind, unblock_condition, next_due))
    ):
        raise ValueError("waiting create requires an exact wait and no action")
