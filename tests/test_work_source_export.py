import hashlib
import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.canonical_work import canonical_metadata
from switchstand.contracts import Routing, WorkContext, WorkPlacement
from switchstand.core import ProviderSourceStory, ProviderStoriesPage, ProviderWork
from switchstand.discovery import ProviderSearchItem, ProviderSearchPage
from switchstand.state import metadata, work_event_handles, work_handles
from switchstand.work_corpus import capture_manifest, compare_parity_exports, write_manifest
from switchstand.work_import import import_parity, target_parity
from switchstand.work_source_export import source_parity

PARENT, CHILD, EVENT = UUID(int=1), UUID(int=2), UUID(int=3)


@pytest.fixture
async def engine(database_prerequisite: None) -> AsyncGenerator[AsyncEngine]:
    del database_prerequisite
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
        await connection.run_sync(metadata.create_all)
        await connection.run_sync(canonical_metadata.create_all)
        await connection.execute(insert(work_handles), [
            {"id": PARENT, "provider": "asana", "provider_work_id": "parent"},
            {"id": CHILD, "provider": "asana", "provider_work_id": "child"},
        ])
        await connection.execute(insert(work_event_handles).values(
            id=EVENT, work_id=CHILD, provider="asana",
            provider_work_id="child", provider_event_id="story-1",
        ))
    yield engine
    await engine.dispose()


def work(gid: str) -> ProviderWork:
    context = WorkContext(
        assignee="Marco",
        placements=(WorkPlacement(area="Project", stage="Doing"),) if gid == "child" else (),
    )
    return ProviderWork(
        gid.title(), f"notes-{gid}", False, f"revision-{gid}",
        Routing(priority="P1", work_type="Implementation"), context, True,
    )


class Source:
    def __init__(self) -> None:
        self.dependencies: dict[str, frozenset[str]] = {
            "child": frozenset(("parent",)), "parent": frozenset(),
        }

    async def search_work(
        self, text: str | None, completed: bool | None,
        cursor: str | None, limit: int,
    ) -> ProviderSearchPage:
        del text, completed, cursor, limit
        return ProviderSearchPage(tuple(
            ProviderSearchItem(gid, item.title, item.completed, item.revision,
                               item.routing, item.context)
            for gid in ("child", "parent") for item in (work(gid),)
        ), None)

    async def get(self, provider_work_id: str) -> ProviderWork | None:
        return work(provider_work_id)

    async def dependencies_for_import(self, provider_work_id: str) -> frozenset[str]:
        return self.dependencies[provider_work_id]

    async def snapshot_for_import(
        self, provider_work_id: str,
    ) -> tuple[ProviderWork, str | None, tuple[tuple[str, str, str | None], ...]] | None:
        return (
            work(provider_work_id), "parent" if provider_work_id == "child" else None,
            (("project-gid", "Project", "Doing"),) if provider_work_id == "child" else (),
        )

    async def source_stories(
        self, provider_task_id: str, observed_revision: str,
        offset: str | None, limit: int, *, require_canonical: bool = True,
    ) -> ProviderStoriesPage | None:
        del limit, require_canonical
        stories: tuple[ProviderSourceStory, ...] = ()
        next_offset = None
        if provider_task_id == "child" and offset is None:
            stories = (ProviderSourceStory(
                "story-2", provider_task_id, "comment_added", "later",
                "2026-10-02T12:00:00Z", "Marco",
            ),)
            next_offset = "next"
        elif provider_task_id == "child":
            stories = (ProviderSourceStory(
                "story-1", provider_task_id, "comment_added", "earlier",
                "2026-10-01T12:00:00Z", "Marco",
            ),)
        return ProviderStoriesPage(
            provider_task_id, observed_revision, stories, next_offset, True,
        )


async def corpus(engine: AsyncEngine, provider: Source, path: Path) -> None:
    write_manifest(path, await capture_manifest(engine, provider, "a" * 40))


async def test_source_export_roundtrips_every_kind_and_is_deterministic(
    engine: AsyncEngine, tmp_path: Path,
):
    provider = Source()
    source_path = tmp_path / "corpus.json"
    await corpus(engine, provider, source_path)

    first = await source_parity(engine, provider, source_path)
    second = await source_parity(engine, provider, source_path)
    assert first == second
    rows = cast(list[dict[str, object]], first["records"])
    records = {
        (cast(str, row["kind"]), cast(str, row["id"])):
        cast(dict[str, object], row["fields"])
        for row in rows
    }
    assert {kind for kind, _ in records} == {
        "work", "alias", "project", "parent", "dependency", "membership", "event",
    }
    assert records[("parent", f'["{CHILD}"]')]["parent_work_id"] == str(PARENT)
    events = [fields for (kind, _), fields in records.items() if kind == "event"]
    assert [(event["sequence"], event["asana_story_gid"]) for event in events] == [
        (1, "story-1"), (2, "story-2"),
    ]
    assert next(event["id"] for event in events if event["asana_story_gid"] == "story-1") == str(EVENT)

    imported = tmp_path / "source-parity.json"
    target = tmp_path / "target-parity.json"
    write_manifest(imported, first)
    assert await import_parity(engine, imported) == len(rows)
    write_manifest(target, await target_parity(engine))
    assert compare_parity_exports(imported, target).records == len(rows)


async def test_source_export_rejects_changed_dependencies_or_incomplete_corpus(
    engine: AsyncEngine, tmp_path: Path,
):
    provider = Source()
    source_path = tmp_path / "corpus.json"
    await corpus(engine, provider, source_path)
    provider.dependencies["child"] = frozenset()
    with pytest.raises(ValueError, match="dependencies changed"):
        await source_parity(engine, provider, source_path)

    broken = tmp_path / "broken.json"
    document = await capture_manifest(engine, Source(), "a" * 40)
    document["exceptions"] = [{"provider_work_id": "gone"}]
    document.pop("sha256")
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    document["sha256"] = hashlib.sha256(canonical).hexdigest()
    write_manifest(broken, document)
    with pytest.raises(ValueError, match="without exceptions"):
        await source_parity(engine, Source(), broken)
