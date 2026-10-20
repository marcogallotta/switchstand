import json
from uuid import uuid4

import pytest

from switchstand.task_control import DurableControlCapsule
from switchstand.task_control_refs import PREFIX, ControlReference, project_references


@pytest.mark.parametrize("kind", ["review_watch", "accepted_obligation"])
def test_reference_roundtrip_is_navigation_not_acceptance(kind: str) -> None:
    ref = ControlReference.model_validate({
        "kind": kind, "work_id": str(uuid4()),
        "package_ref": "git:exact-candidate", "evidence_ref": "message:receipt",
    })
    raw = ("message:receipt", "work:ordinary", ref.encode(), "unfamiliar free text")
    projection = project_references(raw)
    assert projection.references == (ref,)
    assert projection.evidence_status == "UNCHECKED"
    assert projection.coverage == "RECORDED_ONLY"
    assert projection.gaps == ()
    assert "message:receipt" in raw
    assert "complete obligation inventory" in projection.next_action


@pytest.mark.parametrize("payload,reason", [
    ('{"schema_version":2}', "unsupported_version"),
    ("{broken", "invalid_control_reference"),
    ("[]", "invalid_control_reference"),
    ('{"schema_version":1,"kind":"review_watch"}', "invalid_control_reference"),
    ('{"schema_version":true}', "unsupported_version"),
])
def test_only_named_bad_references_produce_gaps(payload: str, reason: str) -> None:
    raw = ("foreign-format:broken", PREFIX + payload, "message:still-present")
    result = project_references(raw)
    assert result.coverage == "PARTIAL"
    assert [(gap.target_index, gap.reason) for gap in result.gaps] == [(1, reason)]
    assert result.references == ()
    assert result.evidence_status == "UNCHECKED"


def test_unknown_fields_cannot_smuggle_verified_evidence() -> None:
    payload = {
        "schema_version": 1, "kind": "accepted_obligation", "work_id": str(uuid4()),
        "package_ref": "exact-package", "evidence_ref": "message:receipt",
        "accepted": True,
    }
    result = project_references((PREFIX + json.dumps(payload),))
    assert result.coverage == "PARTIAL" and not result.references


def test_capsule_preflight_preserves_all_raw_references() -> None:
    ref = ControlReference(
        kind="review_watch", work_id=uuid4(), package_ref="git:head",
        evidence_ref="native:review-occurrence",
    ).encode()
    values = {
        "objective": "deliver", "completion_condition": "verified", "success_proof": "receipt",
        "intended_effect_class": "repository", "progress_summary": "coverage:recorded-only",
        "progress_evidence_ref": "work:anchor", "target_refs": ("message:literal", ref),
    }
    old = DurableControlCapsule.model_validate(values)
    restored = DurableControlCapsule.model_validate_json(old.model_dump_json())
    assert restored.target_refs == ("message:literal", ref)
    with pytest.raises(ValueError, match="64"):
        DurableControlCapsule.model_validate({**values, "target_refs": (ref,) * 65})
    assert old.target_refs == ("message:literal", ref)
