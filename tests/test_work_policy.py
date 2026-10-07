from uuid import uuid4

import pytest

from switchstand.work_policy import (
    SEMANTIC_FIELDS,
    validate_create_state,
    validate_resultant_state,
)


def state(**changes: str | None) -> dict[str, str | None]:
    values: dict[str, str | None] = {
        "lifecycle_state": "CURRENT", "canonical_root": str(uuid4()),
        "owner_key": "agent:root", "wait_kind": "NONE",
        "unblock_condition": "NONE", "next_due": "NONE",
        "next_action_class": "review", "next_action_ref": str(uuid4()),
    }
    values.update(changes)
    return values


@pytest.mark.parametrize("changes", [
    {"canonical_root": None}, {"canonical_root": "{" + str(uuid4()) + "}"},
    {"owner_key": None}, {"next_action_ref": ""},
    {"lifecycle_state": "CURRENT", "wait_kind": "external"},
    {"lifecycle_state": "WAITING", "wait_kind": "", "unblock_condition": "answer",
     "next_action_class": "NONE", "next_action_ref": "NONE"},
    {"lifecycle_state": "WAITING", "wait_kind": "human",
     "unblock_condition": "answer", "next_due": ""},
    {"lifecycle_state": "TERMINAL"},
])
def test_incoherent_resultant_state_is_rejected(changes: dict[str, str | None]) -> None:
    with pytest.raises(ValueError):
        validate_resultant_state(**state(**changes))


@pytest.mark.parametrize("values", [
    state(), state(next_action_class="NONE", next_action_ref="NONE"),
    state(next_action_class="UNKNOWN", next_action_ref="UNKNOWN"),
    state(lifecycle_state="WAITING", wait_kind="human", unblock_condition="answer",
          next_due="UNKNOWN", next_action_class="NONE", next_action_ref="NONE"),
    state(lifecycle_state="DEFERRED", wait_kind="date", unblock_condition="2026-11-01",
          next_due="2026-11-01", next_action_class="NONE", next_action_ref="NONE"),
    state(lifecycle_state="TERMINAL", next_action_class="NONE", next_action_ref="NONE"),
    state(lifecycle_state="UNKNOWN", wait_kind="UNKNOWN", unblock_condition="UNKNOWN",
          next_due="UNKNOWN", next_action_class="UNKNOWN", next_action_ref="UNKNOWN"),
])
def test_coherent_resultant_state_is_accepted(values: dict[str, str | None]) -> None:
    validate_resultant_state(**values)


def test_semantic_field_boundary_excludes_legacy_and_content_fields() -> None:
    assert "review_next_action" not in SEMANTIC_FIELDS
    assert SEMANTIC_FIELDS.isdisjoint({"title", "notes"})


@pytest.mark.parametrize("values", [
    state(),
    state(lifecycle_state="WAITING", wait_kind="human", unblock_condition="answer",
          next_due="2026-10-08", next_action_class="NONE", next_action_ref="NONE"),
    state(lifecycle_state="DEFERRED", wait_kind="date", unblock_condition="2026-11-01",
          next_due="2026-11-01", next_action_class="NONE", next_action_ref="NONE"),
])
def test_substantive_create_requires_complete_action_or_wait(
    values: dict[str, str | None],
) -> None:
    validate_create_state(parented=True, work_type="Task", **values)


def test_ownerless_create_is_only_inert_parented_evidence() -> None:
    values = state(
        owner_key="NONE", next_action_class="NONE", next_action_ref="NONE",
    )
    validate_create_state(parented=True, work_type="Evidence", **values)
    with pytest.raises(ValueError):
        validate_create_state(parented=False, work_type="Evidence", **values)


@pytest.mark.parametrize("changes", [
    {"work_type": "UNKNOWN"},
    {"lifecycle_state": "UNKNOWN"},
    {"owner_key": "UNKNOWN"},
    {"canonical_root": "UNKNOWN"},
    {"next_action_class": "NONE", "next_action_ref": "NONE"},
])
def test_opaque_or_incomplete_substantive_create_is_rejected(
    changes: dict[str, str | None],
) -> None:
    work_type = changes.pop("work_type", "Task")
    with pytest.raises(ValueError):
        validate_create_state(parented=True, work_type=work_type, **state(**changes))
