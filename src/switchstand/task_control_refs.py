"""Navigation hints in existing capsules; never evidence of accepted responsibility."""

from __future__ import annotations

import json
from typing import Literal, cast
from uuid import UUID

from pydantic import Field, ValidationError

from .contracts import ClosedModel

PREFIX = "control-ref:"


class ControlReference(ClosedModel):
    schema_version: Literal[1] = 1
    kind: Literal["review_watch", "accepted_obligation"]
    work_id: UUID
    package_ref: str = Field(min_length=1, max_length=500)
    evidence_ref: str = Field(min_length=1, max_length=1000)

    def encode(self) -> str:
        """Return one ref, still subject to the whole capsule's existing limits."""
        value = PREFIX + self.model_dump_json()
        if len(value) > 2048:
            raise ValueError("control reference exceeds capsule reference limit")
        return value


class ReferenceGap(ClosedModel):
    target_index: int
    reason: Literal["unsupported_version", "invalid_control_reference"]


class ReferenceProjection(ClosedModel):
    references: tuple[ControlReference, ...] = ()
    gaps: tuple[ReferenceGap, ...] = ()
    coverage: Literal["RECORDED_ONLY", "PARTIAL"] = "RECORDED_ONLY"
    evidence_status: Literal["UNCHECKED"] = "UNCHECKED"
    next_action: str = (
        "Read current admitted targets and referenced review/acceptance evidence; "
        "page assigned work separately. Decoding does not establish acceptance, "
        "currentness or a complete obligation inventory."
    )


def project_references(target_refs: tuple[str, ...]) -> ReferenceProjection:
    """Decode only our named format; retain all raw refs in the original capsule."""
    references: list[ControlReference] = []
    gaps: list[ReferenceGap] = []
    for index, value in enumerate(target_refs):
        if not value.startswith(PREFIX):
            continue
        try:
            payload: object = json.loads(value[len(PREFIX):])
            if isinstance(payload, dict) and (
                type(cast(dict[str, object], payload).get("schema_version")) is not int
                or payload["schema_version"] != 1
            ):
                gaps.append(ReferenceGap(target_index=index, reason="unsupported_version"))
                continue
            references.append(ControlReference.model_validate(payload))
        except (ValueError, TypeError, ValidationError, RecursionError):
            gaps.append(ReferenceGap(target_index=index, reason="invalid_control_reference"))
    return ReferenceProjection(
        references=tuple(references), gaps=tuple(gaps),
        coverage="PARTIAL" if gaps else "RECORDED_ONLY",
    )
