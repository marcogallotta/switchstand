"""Temporary deterministic Asana registry resolver for ordinary MCP clients."""

import json
import re
from collections.abc import Awaitable, Callable
from typing import Literal, cast

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
        raise TypeError("resolver payload must be an object with string keys")
    raw_payload = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in raw_payload):
        raise TypeError("resolver payload must be an object with string keys")
    return cast(dict[str, object], raw_payload)


def _entry(payload: dict[str, object], alias: str) -> tuple[str, dict[str, list[str]]]:
    raw = payload.get(alias)
    if not isinstance(raw, dict):
        raise ValueError("resolver alias missing or malformed")
    raw_record = cast(dict[object, object], raw)
    if any(not isinstance(key, str) for key in raw_record):
        raise ValueError("resolver alias missing or malformed")
    record = cast(dict[str, object], raw_record)
    if set(record) != {"owner", "roles"}:
        raise ValueError("resolver alias missing or malformed")
    owner, roles_raw = record["owner"], record["roles"]
    if (
        not isinstance(owner, str)
        or not owner.isdigit()
        or not isinstance(roles_raw, dict)
    ):
        raise ValueError("resolver alias malformed")
    raw_roles = cast(dict[object, object], roles_raw)
    if any(not isinstance(key, str) for key in raw_roles):
        raise ValueError("resolver alias malformed")
    roles = cast(dict[str, object], raw_roles)
    typed: dict[str, list[str]] = {}
    for role, gids_raw in roles.items():
        if not role or not isinstance(gids_raw, list):
            raise ValueError("resolver role malformed")
        gids: list[str] = []
        for gid in cast(list[object], gids_raw):
            if not isinstance(gid, str) or not gid.isdigit():
                raise ValueError("resolver role malformed")
            gids.append(gid)
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
    except (TypeError, ValueError, json.JSONDecodeError):
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
