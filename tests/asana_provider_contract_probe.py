"""Opt-in contract probe for the pinned isolated Asana certification project."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx

from switchstand.certification import (
    CERTIFICATION_PROJECT,
    MARKER,
    FixtureCleaner,
    _verify_runtime,
)
from switchstand.contracts import WorkPatch
from switchstand.core import ProviderRelation
from switchstand.provider import OPT_FIELDS, AsanaProvider

JSON = dict[str, Any]


class ProbeNotRun(RuntimeError):
    """The live boundary prerequisites are absent or not safely attributable."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ProbeNotRun(f"{name} is required")
    return value


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def _project_inventory(provider: AsanaProvider) -> set[str]:
    """Exercise the adapter's real cursor contract with a forced one-row page."""
    found: set[str] = set()
    cursor: str | None = None
    for _ in range(20):
        page = await provider.search_work(None, None, cursor, 1)
        for item in page.items:
            _assert(item.provider_work_id not in found, "search returned a duplicate task")
            found.add(item.provider_work_id)
        cursor = page.next_cursor
        if cursor is None:
            return found
    raise RuntimeError("search pagination exceeded the bounded probe limit")


async def _probe(client: httpx.AsyncClient, project: str, marker: str) -> None:
    cleaner = FixtureCleaner(client, project, marker)
    await cleaner.clean()
    provider = AsanaProvider(
        client, project, test_only=True, create_notes_suffix=marker,
    )
    nonce = uuid4().hex
    try:
        root = await provider.create_work(
            f"provider-contract-root-{nonce}", "probe root", uuid4(), project_gid=project,
        )
        peer = await provider.create_work(
            f"provider-contract-peer-{nonce}", "probe peer", uuid4(), project_gid=project,
        )
        child = await provider.create_work(
            f"provider-contract-child-{nonce}", "probe child", uuid4(),
            parent_task_gid=root,
        )
        raw = await client.get(f"/tasks/{root}", params={"opt_fields": OPT_FIELDS})
        raw.raise_for_status()
        payload = cast(JSON, raw.json()).get("data")
        _assert(isinstance(payload, dict), "exact task response data is not an object")
        _assert(
            isinstance(cast(JSON, payload).get("custom_fields"), list),
            "exact task response omitted the requested custom-field collection",
        )
        work = await provider.get(root)
        _assert(work is not None and work.canonical, "created root is not canonical")
        _assert(marker in work.notes.splitlines(), "created root lost its ownership marker")

        inventory = await _project_inventory(provider)
        _assert({root, peer} <= inventory, "project search did not return both root tasks")

        changed_title = f"provider-contract-updated-{nonce}"
        await provider.update(
            root, WorkPatch(title=changed_title, notes="updated probe", completed=True),
        )
        changed = await provider.get(root)
        _assert(changed is not None, "updated root disappeared")
        _assert(changed.title == changed_title, "title write did not read back")
        _assert(changed.completed is True, "completion write did not read back")
        _assert(changed.notes.splitlines() == ["updated probe", "", marker],
                "notes write or ownership marker did not read back exactly")

        parent = ProviderRelation("parent", "set", target_gid=peer)
        await provider.update_relation(child, parent)
        _assert(await provider.relation_matches(child, parent),
                "parent relation did not read back")

        dependency = ProviderRelation("dependency", "add", target_gid=peer)
        await provider.update_relation(root, dependency)
        _assert(await provider.relation_matches(root, dependency),
                "dependency addition did not read back")
        removal = ProviderRelation("dependency", "remove", target_gid=peer)
        await provider.update_relation(root, removal)
        _assert(await provider.relation_matches(root, removal),
                "dependency removal did not read back")
    finally:
        await cleaner.clean()


async def run() -> None:
    project = _required("SWITCHSTAND_TEST_PROJECT_GID")
    if project != CERTIFICATION_PROJECT:
        raise ProbeNotRun("test project is not the pinned isolated certification project")
    marker = _required("SWITCHSTAND_CERTIFICATION_OWNERSHIP_MARKER")
    if not marker.startswith(MARKER + ":") or len(marker) < 64:
        raise ProbeNotRun("certification ownership marker is invalid")
    root = Path(_required("SWITCHSTAND_CERTIFICATION_REPO")).resolve()
    expected = _required("SWITCHSTAND_CERTIFICATION_RUNTIME_SHA")
    _verify_runtime(root, expected)
    token = _required("ASANA_TOKEN")
    async with httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", trust_env=False,
        headers={"Authorization": f"Bearer {token}"}, timeout=20,
    ) as client:
        await _probe(client, project, marker)
    print(f"PASS isolated Asana provider contract probe candidate={expected}")


def main() -> int:
    try:
        asyncio.run(run())
    except ProbeNotRun as exc:
        print(f"NOT_RUN isolated Asana provider contract probe: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
