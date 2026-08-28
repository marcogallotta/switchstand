"""Pure projection of one complete native B1 tree observation."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence, cast

from .agent_tree import THREAD_SOURCE_KINDS


_SAFE_SUBAGENT_DETAILS = frozenset({"review", "compact", "thread_spawn", "other", "unknown"})


class _ProjectionState:
    """Opaque board-only continuation state; never part of the public projection."""

    __slots__ = (
        "__labels",
        "__issuance_watermark",
        "__active_since",
        "__endpoints",
        "__targets",
    )

    def __init__(
        self,
        labels: dict[str, str],
        issuance_watermark: int,
        active_since: dict[str, float],
        endpoints: dict[str, dict[str, Any]],
        targets: list[tuple[str, str]],
    ) -> None:
        self.__labels = labels
        self.__issuance_watermark = issuance_watermark
        self.__active_since = active_since
        self.__endpoints = endpoints
        self.__targets = targets

    def __repr__(self) -> str:
        return "<_ProjectionState opaque>"


class NativeProjection:
    """Safe public projection with opaque private continuation state."""

    __slots__ = ("agents", "trail", "__state")

    def __init__(
        self,
        agents: list[dict[str, Any]],
        trail: list[dict[str, Any]],
        state: _ProjectionState,
    ) -> None:
        self.agents = agents
        self.trail = trail
        self.__state = state

    def __repr__(self) -> str:
        return f"<NativeProjection agents={len(self.agents)} trail={len(self.trail)}>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, NativeProjection):
            return NotImplemented
        return (
            self.agents == other.agents
            and self.trail == other.trail
            and _private_projection_state(self) == _private_projection_state(other)
        )


def _private_projection_state(
    projection: NativeProjection,
) -> tuple[
    dict[str, str],
    int,
    dict[str, float],
    dict[str, dict[str, Any]],
    list[tuple[str, str]],
]:
    """Expose continuation state only to the board owner within this package."""
    state = cast(
        _ProjectionState,
        object.__getattribute__(projection, "_NativeProjection__state"),
    )
    return (
        cast(dict[str, str], object.__getattribute__(state, "_ProjectionState__labels")),
        cast(
            int,
            object.__getattribute__(state, "_ProjectionState__issuance_watermark"),
        ),
        cast(
            dict[str, float],
            object.__getattribute__(state, "_ProjectionState__active_since"),
        ),
        cast(
            dict[str, dict[str, Any]],
            object.__getattribute__(state, "_ProjectionState__endpoints"),
        ),
        cast(
            list[tuple[str, str]],
            object.__getattribute__(state, "_ProjectionState__targets"),
        ),
    )


def reset_projection_activity(projection: NativeProjection) -> NativeProjection:
    """Preserve historical projection while ending consecutive active evidence."""
    labels, issuance_watermark, _, endpoints, targets = _private_projection_state(projection)
    agents = deepcopy(projection.agents)
    for agent in agents:
        agent["activeObservedSeconds"] = 0.0
    return NativeProjection(
        agents,
        deepcopy(projection.trail),
        _ProjectionState(
            dict(labels), issuance_watermark, {}, deepcopy(endpoints), list(targets)
        ),
    )


def _source(value: Any) -> tuple[str, str | None]:
    if isinstance(value, str):
        return (value if value in THREAD_SOURCE_KINDS else "unknown", None)
    if not isinstance(value, Mapping):
        return "unknown", None
    subagent = value.get("subAgent")
    if isinstance(subagent, str):
        return "subAgent", subagent if subagent in _SAFE_SUBAGENT_DETAILS else "unknown"
    if isinstance(subagent, Mapping) and isinstance(subagent.get("thread_spawn"), Mapping):
        return "subAgent", "thread_spawn"
    return "unknown", None


def _timestamp(thread: Mapping[str, Any], field: str) -> float:
    value = thread.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("native timestamp unavailable")
    return float(value)


def _endpoint(agent: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in agent.items()
        if key not in {"label", "activeObservedSeconds"}
    }


def project_complete_tree(
    threads: Sequence[Mapping[str, Any]],
    *,
    prior_projection: NativeProjection | None = None,
    completed_at: float,
    completed_monotonic: float,
    trail_limit: int,
) -> NativeProjection:
    """Deterministically project one already validated complete pass.

    Acquisition, clocks, locking, persistence, and lifecycle stay with ``NativeBoard``.
    """
    if trail_limit <= 0:
        raise ValueError("trail limit must be positive")
    if prior_projection is None:
        prior_labels: Mapping[str, str] = {}
        issuance_watermark = 1
        prior_active_since: Mapping[str, float] = {}
        prior_endpoints: Mapping[str, Mapping[str, Any]] = {}
        prior_trail: Sequence[Mapping[str, Any]] = []
    else:
        (
            prior_labels,
            issuance_watermark,
            prior_active_since,
            prior_endpoints,
            _,
        ) = _private_projection_state(prior_projection)
        prior_trail = prior_projection.trail

    by_id: dict[str, Mapping[str, Any]] = {}
    for thread in threads:
        thread_id = str(thread["id"])
        if thread_id in by_id:
            raise ValueError("duplicate native target")
        by_id[thread_id] = thread
    labels: dict[str, str] = {}
    for thread_id in by_id:
        ref = prior_labels.get(thread_id)
        if ref is None:
            ref = f"agent-{issuance_watermark}"
            issuance_watermark += 1
        labels[thread_id] = ref
    depth_cache: dict[str, int] = {}

    def depth(thread_id: str) -> int:
        if thread_id in depth_cache:
            return depth_cache[thread_id]
        parent = by_id[thread_id].get("parentThreadId")
        result = 0 if parent is None else depth(str(parent)) + 1
        depth_cache[thread_id] = result
        return result

    present_refs = set(labels.values())
    active_since = {
        ref: started
        for ref, started in prior_active_since.items()
        if ref in present_refs
    }
    agents: list[dict[str, Any]] = []
    targets: list[tuple[str, str]] = []
    for thread in threads:
        thread_id = str(thread["id"])
        ref = labels[thread_id]
        targets.append((ref, thread_id))
        parent_id = thread.get("parentThreadId")
        status = dict(thread["status"])
        status_type = str(status["type"])
        was_active = prior_endpoints.get(ref, {}).get("status") == "active"
        if status_type == "active":
            if not was_active or ref not in active_since:
                active_since[ref] = completed_monotonic
            active_seconds = completed_monotonic - active_since[ref]
        else:
            active_since.pop(ref, None)
            active_seconds = 0.0
        source_kind, source_detail = _source(thread.get("source"))
        agents.append(
            {
                "agentRef": ref,
                "label": f"Agent {ref.removeprefix('agent-')}",
                "parentRef": labels[str(parent_id)] if parent_id is not None else None,
                "depth": depth(thread_id),
                "sourceKind": source_kind,
                "sourceDetail": source_detail,
                "createdAt": _timestamp(thread, "createdAt"),
                "updatedAt": _timestamp(thread, "updatedAt"),
                "status": status_type,
                "activeFlags": (
                    list(status.get("activeFlags", [])) if status_type == "active" else []
                ),
                "activeObservedSeconds": round(active_seconds, 3),
            }
        )

    endpoints = {str(agent["agentRef"]): _endpoint(agent) for agent in agents}
    entries: list[dict[str, Any]] = []
    for ref in sorted(set(prior_endpoints) | set(endpoints)):
        before, after = prior_endpoints.get(ref), endpoints.get(ref)
        if before is None or after is None:
            changes = {
                "presence": {
                    "from": "absent" if before is None else "present",
                    "to": "present" if after is not None else "absent",
                }
            }
        else:
            changes = {
                key: {"from": before.get(key), "to": after.get(key)}
                for key in sorted(set(before) | set(after))
                if before.get(key) != after.get(key)
            }
        if changes:
            entries.append({"observedAt": completed_at, "agentRef": ref, "changes": changes})
    trail = (deepcopy([dict(entry) for entry in prior_trail]) + entries)[-trail_limit:]
    return NativeProjection(
        agents,
        trail,
        _ProjectionState(labels, issuance_watermark, active_since, endpoints, targets),
    )
