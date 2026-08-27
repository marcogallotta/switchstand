"""Pure current-selection gates and opaque in-process native targets."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .native_selection import resolve_native_selection


class ExactCurrentTarget:
    """Equality-capable private target whose native identity is never displayed."""

    __slots__ = ("__identity",)

    def __init__(self) -> None:
        self.__identity = object()

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, ExactCurrentTarget)
            and self.__identity is other.__identity
        )

    def __hash__(self) -> int:
        return hash(self.__identity)

    def __repr__(self) -> str:
        return "<ExactCurrentTarget opaque>"


@dataclass(frozen=True)
class PrivateTargetRecord:
    """One private mapping entry from a complete board pass."""

    agent_ref: str
    target: ExactCurrentTarget


def resolve_exact_current_target(
    selection: Any,
    observation: Any,
    target_records: Any,
    *,
    now: Any,
    maximum_observation_age_seconds: Any,
) -> dict[str, Any] | ExactCurrentTarget:
    """Resolve one exact pair through the frozen public gates and private index."""
    resolved = resolve_native_selection(
        selection,
        observation,
        now=now,
        maximum_observation_age_seconds=maximum_observation_age_seconds,
    )
    if "code" in resolved:
        return resolved
    agent_ref = resolved["agentRef"]
    if not isinstance(target_records, (list, tuple)):
        return resolve_native_selection(
            None,
            observation,
            now=now,
            maximum_observation_age_seconds=maximum_observation_age_seconds,
        )
    matches = [
        record.target
        for record in target_records
        if isinstance(record, PrivateTargetRecord) and record.agent_ref == agent_ref
    ]
    if len(matches) == 1:
        return matches[0]
    return resolve_native_selection(
        None,
        observation,
        now=now,
        maximum_observation_age_seconds=maximum_observation_age_seconds,
    )


def browser_selection_shape(
    selection: Any,
    observation: Any,
    *,
    now: Any,
    maximum_observation_age_seconds: Any,
) -> dict[str, Any]:
    """Return only an exact safe pair and its frozen selection snapshot/error."""
    identity = (
        {
            "observationRunRef": selection.get("observationRunRef"),
            "agentRef": selection.get("agentRef"),
        }
        if isinstance(selection, Mapping)
        else {}
    )
    snapshot = resolve_native_selection(
        selection,
        observation,
        now=now,
        maximum_observation_age_seconds=maximum_observation_age_seconds,
    )
    if snapshot.get("code") == "INVALID_AGENT_REF":
        identity = None
    elif "version" in snapshot:
        identity = {
            "observationRunRef": snapshot["observationRunRef"],
            "agentRef": snapshot["agentRef"],
        }
    return {"selection": identity, "snapshot": snapshot}
