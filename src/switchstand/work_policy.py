"""Resultant-state policy for canonical work semantic writes."""

from uuid import UUID

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
) -> None:
    """Reject incomplete or incoherent state after a semantic write."""
    if lifecycle_state not in {"CURRENT", "WAITING", "DEFERRED", "TERMINAL", "UNKNOWN"}:
        raise ValueError("semantic writes require a lifecycle state")
    if not _root(canonical_root):
        raise ValueError("canonical root must be NONE, UNKNOWN, or a WorkId")
    if not owner_key:
        raise ValueError("owner key must be NONE, UNKNOWN, or nonempty")

    action = (next_action_class, next_action_ref)
    if action not in {("NONE", "NONE"), ("UNKNOWN", "UNKNOWN")} and not all(
        _exact(value) for value in action
    ):
        raise ValueError("next action class and reference must be coupled")

    if lifecycle_state == "CURRENT":
        if (wait_kind, unblock_condition, next_due) != ("NONE", "NONE", "NONE"):
            raise ValueError("current work cannot carry a wait")
    elif lifecycle_state in {"WAITING", "DEFERRED"}:
        if not _exact(wait_kind) or not _exact(unblock_condition):
            raise ValueError("waiting/deferred work requires an exact wait and unblock")
        if action != ("NONE", "NONE"):
            raise ValueError("waiting/deferred work cannot carry a next action")
        if not next_due:
            raise ValueError("waiting/deferred work requires an explicit due disposition")
    elif lifecycle_state == "TERMINAL":
        if (wait_kind, unblock_condition, next_due) != ("NONE", "NONE", "NONE"):
            raise ValueError("terminal work cannot carry a wait")
        if action != ("NONE", "NONE"):
            raise ValueError("terminal work cannot carry a next action")
    elif not all((wait_kind, unblock_condition, next_due)):
        raise ValueError("unknown-lifecycle work requires explicit wait dispositions")
