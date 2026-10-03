"""Temporary frozen Asana-to-canonical parity exporter."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .canonical_work import normalize_title
from .core import ProviderSourceStory, ProviderStoriesPage, ProviderWork
from .provider import AsanaProvider
from .state import work_event_handles, work_handles
from .work_corpus import load_manifest, parity_manifest, parity_value, write_manifest

Placement = tuple[str, str, str | None]
TOMBSTONE_EVENT_TIME = datetime(1970, 1, 1, tzinfo=UTC)


class SourceProvider(Protocol):
    async def snapshot_for_import(
        self, provider_work_id: str,
    ) -> tuple[ProviderWork, str | None, tuple[Placement, ...]] | None: ...

    async def dependencies_for_import(self, provider_work_id: str) -> frozenset[str]: ...

    async def source_stories(
        self, provider_task_id: str, observed_revision: str,
        offset: str | None, limit: int, *, require_canonical: bool = True,
    ) -> ProviderStoriesPage | None: ...


def _identity(*values: object) -> str:
    return json.dumps([str(value) for value in values], separators=(",", ":"))


def _record(kind: str, identity: str, fields: dict[str, object]) -> dict[str, object]:
    return {"kind": kind, "id": identity, "fields": fields}


def _current_rows(manifest: dict[str, object]) -> tuple[
    dict[str, dict[str, object]], dict[str, dict[str, object]],
]:
    rows, exceptions = manifest.get("rows"), manifest.get("exceptions")
    if not isinstance(rows, list) or not isinstance(exceptions, list):
        raise TypeError("source corpus rows or exceptions are invalid")
    current: dict[str, dict[str, object]] = {}
    for raw in cast(list[object], rows):
        if not isinstance(raw, dict):
            raise TypeError("source corpus row must be an object")
        row = cast(dict[str, object], raw)
        gid, work_id = row.get("provider_work_id"), row.get("work_id")
        dependencies = row.get("dependencies")
        if work_id is None:
            continue
        if (
            not isinstance(gid, str) or not gid or gid in current
            or not isinstance(work_id, str) or not isinstance(row.get("revision"), str)
            or not isinstance(dependencies, list)
            or any(not isinstance(value, str) for value in cast(list[object], dependencies))
        ):
            raise ValueError("source corpus identity is invalid")
        UUID(work_id)
        current[gid] = row
    if not current or len({row["work_id"] for row in current.values()}) != len(current):
        raise ValueError("source corpus must map one current task to each WorkId")
    excluded: dict[str, dict[str, object]] = {}
    for raw in cast(list[object], exceptions):
        if not isinstance(raw, dict):
            raise TypeError("source corpus exception must be an object")
        item = cast(dict[str, object], raw)
        gid, work_id, reason = (
            item.get("provider_work_id"), item.get("work_id"), item.get("reason")
        )
        if (
            set(item) != {"provider_work_id", "work_id", "reason"}
            or not isinstance(gid, str) or not gid or gid in excluded or gid in current
            or not isinstance(work_id, str) or not isinstance(reason, str)
        ):
            raise ValueError("source corpus exception identity is invalid")
        UUID(work_id)
        excluded[gid] = item
    return current, excluded


def _tombstones(
    path: Path | None, corpus: dict[str, object], excluded: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    if path is None:
        if excluded:
            raise ValueError("source corpus exceptions require a reviewed tombstone disposition")
        return {}
    document = load_manifest(path)
    if set(document) != {"schema_version", "corpus_sha256", "tombstones", "sha256"}:
        raise ValueError("tombstone disposition schema is invalid")
    if document["schema_version"] != 1 or document["corpus_sha256"] != corpus["sha256"]:
        raise ValueError("tombstone disposition does not bind this source corpus")
    raw_tombstones = document["tombstones"]
    if not isinstance(raw_tombstones, list):
        raise TypeError("tombstones must be a list")
    result: dict[str, dict[str, object]] = {}
    for raw in cast(list[object], raw_tombstones):
        if not isinstance(raw, dict):
            raise TypeError("tombstone disposition must be an object")
        item = cast(dict[str, object], raw)
        expected_keys = {
            "provider_work_id", "work_id", "source_reason", "disposition",
            "title", "notes", "event_aliases",
        }
        gid, work_id = item.get("provider_work_id"), item.get("work_id")
        aliases = item.get("event_aliases")
        if (
            set(item) != expected_keys or not isinstance(gid, str) or not gid
            or gid in result or not isinstance(work_id, str)
            or item.get("disposition") != "retired_tombstone"
            or not isinstance(item.get("title"), str) or not item["title"]
            or not isinstance(item.get("notes"), str) or not isinstance(aliases, list)
        ):
            raise ValueError("tombstone disposition entry is invalid")
        UUID(work_id)
        expected = excluded.get(gid)
        if (
            expected is None or expected["work_id"] != work_id
            or expected["reason"] != item.get("source_reason")
            or expected["reason"] not in {"missing", "noncanonical"}
        ):
            raise ValueError("tombstone disposition does not match a retired corpus exception")
        normalized_aliases: list[dict[str, str]] = []
        seen: set[tuple[UUID, str]] = set()
        for raw_alias in cast(list[object], aliases):
            if not isinstance(raw_alias, dict):
                raise TypeError("tombstone event alias must be an object")
            alias = cast(dict[str, object], raw_alias)
            if set(alias) != {"event_id", "story_gid"}:
                raise ValueError("tombstone event alias schema is invalid")
            event_id, story_gid = alias["event_id"], alias["story_gid"]
            if not isinstance(event_id, str) or not isinstance(story_gid, str) or not story_gid:
                raise ValueError("tombstone event alias identity is invalid")
            key = UUID(event_id), story_gid
            if key in seen:
                raise ValueError("duplicate tombstone event alias")
            seen.add(key)
            normalized_aliases.append({"event_id": event_id, "story_gid": story_gid})
        result[gid] = item | {"event_aliases": normalized_aliases}
    if set(result) != set(excluded):
        raise ValueError("tombstone disposition must cover every source corpus exception")
    return result


async def _stories(
    provider: SourceProvider, gid: str, revision: str,
) -> list[ProviderSourceStory]:
    stories: list[ProviderSourceStory] = []
    offset: str | None = None
    seen: set[str] = set()
    while True:
        page = await provider.source_stories(gid, revision, offset, 100)
        if (
            page is None or page.stale or not page.canonical or page.revision != revision
            or page.task_gid != gid
        ):
            raise ValueError(f"source history changed or became unavailable: {gid}")
        stories.extend(page.stories)
        offset = page.next_offset
        if offset is None:
            return stories
        if offset in seen:
            raise ValueError(f"source history repeated a cursor: {gid}")
        seen.add(offset)


async def source_parity(
    engine: AsyncEngine, provider: SourceProvider, corpus_path: Path,
    tombstone_path: Path | None = None,
) -> dict[str, object]:
    """Export every current source field into the canonical parity schema."""
    corpus = load_manifest(corpus_path)
    rows, excluded = _current_rows(corpus)
    tombstones = _tombstones(tombstone_path, corpus, excluded)
    async with engine.connect() as connection:
        bindings = (await connection.execute(select(
            work_handles.c.provider_work_id, work_handles.c.id,
        ).where(work_handles.c.provider == "asana"))).all()
        event_rows = (await connection.execute(select(
            work_event_handles.c.id, work_event_handles.c.work_id,
            work_event_handles.c.provider_work_id, work_event_handles.c.provider_event_id,
        ).where(work_event_handles.c.provider == "asana"))).all()
    expected = {gid: UUID(cast(str, row["work_id"])) for gid, row in rows.items()}
    expected.update({
        gid: UUID(cast(str, row["work_id"])) for gid, row in tombstones.items()
    })
    if {gid: work_id for gid, work_id in bindings} != expected:
        raise ValueError("source corpus no longer matches current Asana bindings")
    event_ids = {(work_id, task_gid, story_gid): event_id
                 for event_id, work_id, task_gid, story_gid in event_rows}
    expected_event_ids = {
        event_id for (work_id, _, _), event_id in event_ids.items()
        if work_id in set(expected.values())
    }
    consumed_event_ids: set[UUID] = set()
    records: list[dict[str, object]] = []
    projects: dict[str, tuple[UUID, str]] = {}
    used_events: dict[UUID, tuple[UUID, str]] = {}
    for gid, tombstone in sorted(tombstones.items()):
        work_id = expected[gid]
        records.extend((
            _record("work", _identity(work_id), {
                "work_id": str(work_id), "title": tombstone["title"],
                "normalized_title": normalize_title(cast(str, tombstone["title"])),
                "completed": True, "notes": tombstone["notes"], "assignee": None,
                "priority": None, "work_type": None, "lifecycle_state": None,
                "review_next_action": None, "wait_kind": None,
                "unblock_condition": None, "next_due": None, "row_version": 1,
            }),
            _record("alias", _identity(gid), {
                "asana_task_gid": gid, "work_id": str(work_id),
            }),
        ))
        actual_aliases = sorted(
            (str(event_id), story_gid)
            for event_id, event_work_id, task_gid, story_gid in event_rows
            if event_work_id == work_id and task_gid == gid
        )
        reviewed_aliases = sorted(
            (alias["event_id"], alias["story_gid"])
            for alias in cast(list[dict[str, str]], tombstone["event_aliases"])
        )
        if actual_aliases != reviewed_aliases:
            raise ValueError(f"tombstone event aliases changed: {gid}")
        for sequence, (event_id_text, story_gid) in enumerate(reviewed_aliases, 1):
            event_id = UUID(event_id_text)
            consumed_event_ids.add(event_id)
            if event_id in used_events:
                raise ValueError("source event UUID collision")
            used_events[event_id] = work_id, story_gid
            records.append(_record("event", _identity(event_id), {
                "id": str(event_id), "work_id": str(work_id), "sequence": sequence,
                "result_version": 1, "subtype": "historical_alias", "text": None,
                "created_at": parity_value(TOMBSTONE_EVENT_TIME), "actor": None,
                "asana_story_gid": story_gid, "operation_id": None,
            }))
    for gid, row in sorted(rows.items()):
        work_id, revision = expected[gid], cast(str, row["revision"])
        snapshot = await provider.snapshot_for_import(gid)
        if snapshot is None:
            raise ValueError(f"source work became unavailable: {gid}")
        work, parent_gid, placements = snapshot
        if not work.canonical or work.revision != revision:
            raise ValueError(f"source work changed or became noncanonical: {gid}")
        dependencies = await provider.dependencies_for_import(gid)
        if dependencies != frozenset(cast(list[str], row["dependencies"])):
            raise ValueError(f"source dependencies changed: {gid}")
        routing, context = work.routing, work.context
        fields: dict[str, object] = {
            "work_id": str(work_id), "title": work.title,
            "normalized_title": normalize_title(work.title), "completed": work.completed,
            "notes": work.notes, "assignee": context.assignee, "priority": routing.priority,
            "work_type": routing.work_type, "lifecycle_state": routing.lifecycle_state,
            "review_next_action": routing.review_next_action, "wait_kind": routing.wait_kind,
            "unblock_condition": routing.unblock_condition, "next_due": routing.next_due,
            "row_version": 1,
        }
        records.extend((
            _record("work", _identity(work_id), fields),
            _record("alias", _identity(gid), {"asana_task_gid": gid, "work_id": str(work_id)}),
        ))
        if parent_gid is not None:
            if parent_gid not in expected:
                raise ValueError(f"parent is outside the current corpus: {gid}")
            records.append(_record("parent", _identity(work_id), {
                "child_work_id": str(work_id), "parent_work_id": str(expected[parent_gid]),
            }))
        for dependency in sorted(dependencies):
            if dependency not in expected:
                raise ValueError(f"dependency is outside the current corpus: {gid}")
            records.append(_record("dependency", _identity(work_id, expected[dependency]), {
                "work_id": str(work_id), "depends_on_work_id": str(expected[dependency]),
            }))
        for project_gid, name, section in placements:
            project_id = uuid5(NAMESPACE_URL, f"switchstand:asana-project:{project_gid}")
            if project_gid in projects and projects[project_gid] != (project_id, name):
                raise ValueError(f"project identity changed: {project_gid}")
            projects[project_gid] = project_id, name
            records.append(_record("membership", _identity(project_id, work_id), {
                "project_id": str(project_id), "work_id": str(work_id), "section_name": section,
            }))
        stories = await _stories(provider, gid, revision)
        for sequence, story in enumerate(sorted(
            stories, key=lambda item: (item.created_at, item.story_gid),
        ), 1):
            if story.task_gid != gid or not story.subtype:
                raise ValueError(f"source story is invalid: {gid}")
            event_id = event_ids.get((work_id, gid, story.story_gid))
            if event_id is not None:
                consumed_event_ids.add(event_id)
            else:
                event_id = uuid5(NAMESPACE_URL, f"switchstand:asana-story:{story.story_gid}")
            if event_id in used_events and used_events[event_id] != (work_id, story.story_gid):
                raise ValueError("source event UUID collision")
            used_events[event_id] = work_id, story.story_gid
            records.append(_record("event", _identity(event_id), {
                "id": str(event_id), "work_id": str(work_id), "sequence": sequence,
                "result_version": 1, "subtype": story.subtype, "text": story.text,
                "created_at": parity_value(datetime.fromisoformat(story.created_at)),
                "actor": story.created_by, "asana_story_gid": story.story_gid,
                "operation_id": None,
            }))
    if consumed_event_ids != expected_event_ids:
        raise ValueError("source history does not contain every existing Asana event alias")
    records.extend(_record("project", _identity(project_id), {
        "project_id": str(project_id), "asana_project_gid": gid, "name": name,
    }) for gid, (project_id, name) in sorted(projects.items()))
    return parity_manifest(records)


async def _export(corpus: Path, output: Path, tombstones: Path | None = None) -> int:
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", trust_env=False,
        headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"},
    )
    try:
        manifest = await source_parity(engine, AsanaProvider(client), corpus, tombstones)
        write_manifest(output, manifest)
        return len(cast(Sequence[object], manifest["records"]))
    finally:
        await client.aclose()
        await engine.dispose()


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tombstones", type=Path)
    arguments = parser.parse_args(argv)
    try:
        print(f"source_records={asyncio.run(_export(
            arguments.corpus, arguments.output, arguments.tombstones,
        ))}")
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.exit(1, f"Work source export failed: {error}\n")


if __name__ == "__main__":
    run()
