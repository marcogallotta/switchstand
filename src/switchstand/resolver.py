"""Temporary deterministic Asana registry resolver for ordinary MCP clients."""

import json
import re
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import Field, model_validator

from .contracts import ClosedModel, SourceTask, SourceTaskResult

REGISTRY_TASK_GID = "1218432271807843"
MARKER = "MCP_RESOLVER_V1"
Alias = str
TaskReader = Callable[[str], Awaitable[SourceTaskResult]]


class ResolvedTask(ClosedModel):
    task_gid: str = Field(pattern=r"^[0-9]+$")
    title: str
    completed: bool
    revision: str


class ResolverHit(ClosedModel):
    owner: ResolvedTask
    roles: dict[str, tuple[ResolvedTask, ...]]


class ResolverResult(ClosedModel):
    status: Literal["ok", "unknown", "denied", "provider_error"]
    alias: str | None = None
    hit: ResolverHit | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "ok") != (self.hit is not None):
            raise ValueError("resolver hit presence does not match status")
        return self


def normalize_alias(value: str) -> Alias:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if not normalized or len(normalized) > 80:
        raise ValueError("invalid resolver alias")
    return normalized


def _payload(notes: str) -> dict[str, object]:
    marker = notes.find(MARKER)
    if marker < 0:
        raise ValueError("resolver marker missing")
    tail = notes[marker + len(MARKER):].lstrip()
    if tail.startswith("```"):
        newline = tail.find("\n")
        if newline < 0:
            raise ValueError("resolver block missing")
        tail = tail[newline + 1:]
    value, _ = json.JSONDecoder().raw_decode(tail)
    if not isinstance(value, dict):
        raise ValueError("resolver payload must be an object")
    return value


def _entry(payload: dict[str, object], alias: str) -> tuple[str, dict[str, list[str]]]:
    raw = payload.get(alias)
    if not isinstance(raw, dict) or set(raw) != {"owner", "roles"}:
        raise ValueError("resolver alias missing or malformed")
    owner, roles = raw["owner"], raw["roles"]
    if not isinstance(owner, str) or not owner.isdigit() or not isinstance(roles, dict):
        raise ValueError("resolver alias malformed")
    typed: dict[str, list[str]] = {}
    for role, gids in roles.items():
        if (
            not isinstance(role, str) or not role
            or not isinstance(gids, list)
            or any(not isinstance(gid, str) or not gid.isdigit() for gid in gids)
        ):
            raise ValueError("resolver role malformed")
        typed[role] = gids
    all_gids = [owner, *(gid for gids in typed.values() for gid in gids)]
    if len(all_gids) != len(set(all_gids)):
        raise ValueError("resolver contains contradictory duplicate roles")
    return owner, typed


async def resolve_alias(reference: str, reader: TaskReader) -> ResolverResult:
    try:
        alias = normalize_alias(reference)
    except ValueError:
        return ResolverResult(status="unknown")
    registry = await reader(REGISTRY_TASK_GID)
    if registry.status != "ok" or registry.item is None:
        return ResolverResult(status=registry.status, alias=alias)
    try:
        owner_gid, roles = _entry(_payload(registry.item.notes), alias)
    except (ValueError, json.JSONDecodeError):
        return ResolverResult(status="unknown", alias=alias)

    async def exact(gid: str) -> ResolvedTask | None:
        result = await reader(gid)
        if result.status != "ok" or result.item is None:
            return None
        item: SourceTask = result.item
        return ResolvedTask(
            task_gid=item.task_gid, title=item.title, completed=item.completed,
            revision=item.revision,
        )

    owner = await exact(owner_gid)
    if owner is None:
        return ResolverResult(status="unknown", alias=alias)
    resolved_roles: dict[str, tuple[ResolvedTask, ...]] = {}
    for role, gids in roles.items():
        resolved: list[ResolvedTask] = []
        for gid in gids:
            task = await exact(gid)
            if task is None:
                return ResolverResult(status="unknown", alias=alias)
            resolved.append(task)
        resolved_roles[role] = tuple(resolved)
    return ResolverResult(
        status="ok", alias=alias, hit=ResolverHit(owner=owner, roles=resolved_roles)
    )
