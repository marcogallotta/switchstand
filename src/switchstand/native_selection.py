"""Pure, fail-closed resolution for the ``native-selection-v1`` boundary."""
from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Mapping, cast


NATIVE_SELECTION_VERSION = "native-selection-v1"
SAFE_DISPLAY_PROVENANCE = "explicitly-safe"

_ERROR_MESSAGES = MappingProxyType(
    {
        "INVALID_AGENT_REF": "Selected agent is unavailable.",
        "APP_SERVER_DISCONNECTED": "Agent connection is unavailable.",
        "OBSERVATION_STALE": "Selected agent observation is stale.",
        "AGENT_NOT_PRESENT": "Selected agent is no longer present.",
    }
)
_IDENTITY_FIELDS = frozenset({"observationRunRef", "agentRef"})
_REQUIRED_DTO_FIELDS = frozenset(
    {"version", "observationRunRef", "agentRef", "connected", "present"}
)
_OPTIONAL_DTO_FIELDS = frozenset({"name", "agentNickname"})


def _error(code: str) -> dict[str, str]:
    return {"code": code, "message": _ERROR_MESSAGES[code]}


def _opaque_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _matching_record(
    observation: Mapping[str, Any], agent_ref: str
) -> Mapping[str, Any] | None:
    records = observation.get("agentRecords")
    if not isinstance(records, list):
        return None
    matches = [
        record
        for record in records
        if isinstance(record, Mapping) and record.get("agentRef") == agent_ref
    ]
    return matches[0] if len(matches) == 1 else None


def _safe_display_value(record: Mapping[str, Any], field: str) -> str | None:
    candidate = record.get(field)
    if not isinstance(candidate, Mapping) or set(candidate) != {"value", "provenance"}:
        return None
    value = candidate.get("value")
    if candidate.get("provenance") != SAFE_DISPLAY_PROVENANCE or not isinstance(value, str):
        return None
    return value


def validate_native_selection_v1(value: Any) -> dict[str, Any]:
    """Return a closed DTO copy or reject it without disclosing field details."""
    if not isinstance(value, Mapping):
        raise ValueError("invalid native-selection-v1 DTO")
    keys = set(value)
    if not _REQUIRED_DTO_FIELDS <= keys or not keys <= (
        _REQUIRED_DTO_FIELDS | _OPTIONAL_DTO_FIELDS
    ):
        raise ValueError("invalid native-selection-v1 DTO")
    if (
        value.get("version") != NATIVE_SELECTION_VERSION
        or not _opaque_ref(value.get("observationRunRef"))
        or not _opaque_ref(value.get("agentRef"))
        or value.get("connected") is not True
        or value.get("present") is not True
    ):
        raise ValueError("invalid native-selection-v1 DTO")
    for field in _OPTIONAL_DTO_FIELDS & keys:
        if not isinstance(value.get(field), str):
            raise ValueError("invalid native-selection-v1 DTO")
    return {field: value[field] for field in value}


def resolve_native_selection(
    selection: Any,
    observation: Any,
    *,
    now: Any,
    maximum_observation_age_seconds: Any,
) -> dict[str, Any]:
    """Resolve one exact run-local identity from supplied complete B1 observation state.

    The function performs no I/O and reads no clock. Callers supply both ``now`` and the
    configured freshness maximum. Resolution failures follow the issue-defined precedence.
    Extra observation data is deliberately ignored; the returned DTO is independently closed.
    """
    if not isinstance(selection, Mapping) or set(selection) != _IDENTITY_FIELDS:
        return _error("INVALID_AGENT_REF")
    observation_run_ref = selection.get("observationRunRef")
    agent_ref = selection.get("agentRef")
    if not _opaque_ref(observation_run_ref) or not _opaque_ref(agent_ref):
        return _error("INVALID_AGENT_REF")
    observation_run_ref = cast(str, observation_run_ref)
    agent_ref = cast(str, agent_ref)
    if not isinstance(observation, Mapping):
        return _error("INVALID_AGENT_REF")
    if observation.get("observationRunRef") != observation_run_ref:
        return _error("INVALID_AGENT_REF")
    record = _matching_record(observation, agent_ref)
    if record is None:
        return _error("INVALID_AGENT_REF")

    if observation.get("connected") is not True:
        return _error("APP_SERVER_DISCONNECTED")

    completed_at = _finite_number(observation.get("latestCompletePassCompletedAt"))
    current_time = _finite_number(now)
    maximum_age = _finite_number(maximum_observation_age_seconds)
    if (
        completed_at is None
        or current_time is None
        or maximum_age is None
        or maximum_age < 0
        or not 0 <= current_time - completed_at <= maximum_age
    ):
        return _error("OBSERVATION_STALE")

    if record.get("present") is not True:
        return _error("AGENT_NOT_PRESENT")

    result: dict[str, Any] = {
        "version": NATIVE_SELECTION_VERSION,
        "observationRunRef": observation_run_ref,
        "agentRef": agent_ref,
        "connected": observation["connected"],
        "present": record["present"],
    }
    for field in ("name", "agentNickname"):
        display_value = _safe_display_value(record, field)
        if display_value is not None:
            result[field] = display_value
    return validate_native_selection_v1(result)
