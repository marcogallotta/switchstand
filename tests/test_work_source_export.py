import hashlib
import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.canonical_work import canonical_metadata, canonical_work
from switchstand.contracts import Routing, WorkContext, WorkPlacement
from switchstand.core import ProviderSourceStory, ProviderStoriesPage, ProviderWork
from switchstand.discovery import ProviderSearchItem, ProviderSearchPage
from switchstand.provider import ProviderWorkDecodeError
from switchstand.state import metadata, work_event_handles, work_handles
from switchstand.work_corpus import (
    capture_manifest,
    compare_parity_exports,
    parity_manifest,
    write_manifest,
)
from switchstand.work_import import import_parity, target_parity
from switchstand.work_source_export import source_parity

PARENT, CHILD, EVENT, MISSING_EVENT = UUID(int=1), UUID(int=2), UUID(int=3), UUID(int=4)
TOMBSTONE, TOMBSTONE_EVENT = UUID(int=5), UUID(int=6)


@pytest.fixture
async def engine(database_prerequisite: None) -> AsyncGenerator[AsyncEngine]:
    del database_prerequisite
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.execute(text("TRUNCATE work_handles CASCADE"))
        await connection.run_sync(canonical_metadata.create_all)
        await connection.execute(insert(work_handles), [
            {"id": PARENT, "provider": "asana", "provider_work_id": "parent"},
            {"id": CHILD, "provider": "asana", "provider_work_id": "child"},
        ])
        await connection.execute(insert(work_event_handles).values(
            id=EVENT, work_id=CHILD, provider="asana",
            provider_work_id="child", provider_event_id="story-1",
        ))
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(canonical_metadata.drop_all)
            await connection.execute(text("TRUNCATE work_handles CASCADE"))
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

    async def has_zero_memberships(self, _provider_work_id: str) -> bool:
        return False

    async def dependencies_for_import(self, provider_work_id: str) -> frozenset[str]:
        return self.dependencies[provider_work_id]

    async def snapshot_for_import(
        self, provider_work_id: str,
    ) -> tuple[
        ProviderWork, str | None, tuple[tuple[str, str, str | None], ...], bool,
    ] | None:
        return (
            work(provider_work_id), "parent" if provider_work_id == "child" else None,
            (("project-gid", "Project", "Doing"),) if provider_work_id == "child" else (),
            False,
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


def digested(document: dict[str, object]) -> dict[str, object]:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return document | {"sha256": hashlib.sha256(canonical).hexdigest()}


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
    with pytest.raises(ValueError, match="exception identity"):
        await source_parity(engine, Source(), broken)

    provider.dependencies["child"] = frozenset(("parent",))
    async with engine.begin() as connection:
        await connection.execute(insert(work_event_handles).values(
            id=MISSING_EVENT, work_id=CHILD, provider="asana",
            provider_work_id="child", provider_event_id="missing-story",
        ))
    with pytest.raises(ValueError, match="every existing Asana event alias"):
        await source_parity(engine, provider, source_path)


async def test_reviewed_tombstone_roundtrips_work_alias_and_known_event_aliases(
    engine: AsyncEngine, tmp_path: Path,
):
    async with engine.begin() as connection:
        await connection.execute(insert(work_handles).values(
            id=TOMBSTONE, provider="asana", provider_work_id="retired",
        ))
        await connection.execute(insert(work_event_handles).values(
            id=TOMBSTONE_EVENT, work_id=TOMBSTONE, provider="asana",
            provider_work_id="retired", provider_event_id="retired-story",
        ))

    class WithMissingTombstone(Source):
        async def get(self, provider_work_id: str) -> ProviderWork | None:
            return None if provider_work_id == "retired" else await super().get(provider_work_id)

    provider = WithMissingTombstone()
    corpus_path = tmp_path / "corpus.json"
    await corpus(engine, provider, corpus_path)
    source_corpus = json.loads(corpus_path.read_text())
    disposition_path = tmp_path / "tombstones.json"
    write_manifest(disposition_path, digested({
        "schema_version": 1,
        "corpus_sha256": source_corpus["sha256"],
        "tombstones": [{
            "provider_work_id": "retired", "work_id": str(TOMBSTONE),
            "source_reason": "missing", "disposition": "retired_tombstone",
            "title": "Retired proof", "notes": "Intentionally retained as a tombstone.",
            "event_aliases": [{
                "event_id": str(TOMBSTONE_EVENT), "story_gid": "retired-story",
            }],
        }],
    }))

    exported = await source_parity(engine, provider, corpus_path, disposition_path)
    records = cast(list[dict[str, object]], exported["records"])
    tombstone = next(cast(dict[str, object], row["fields"]) for row in records
                     if row["kind"] == "work" and row["id"] == f'["{TOMBSTONE}"]')
    event = next(cast(dict[str, object], row["fields"]) for row in records
                 if row["kind"] == "event" and row["id"] == f'["{TOMBSTONE_EVENT}"]')
    assert tombstone == {
        "work_id": str(TOMBSTONE), "title": "Retired proof",
        "normalized_title": "retired proof", "completed": True,
        "notes": "Intentionally retained as a tombstone.", "assignee": None,
        "priority": None, "work_type": None, "lifecycle_state": None,
        "review_next_action": None, "wait_kind": None, "unblock_condition": None,
        "next_due": None, "row_version": 1,
    }
    assert event["id"] == str(TOMBSTONE_EVENT)
    assert event["asana_story_gid"] == "retired-story"
    assert event["subtype"] == "historical_alias"

    source_path, target_path = tmp_path / "source.json", tmp_path / "target.json"
    write_manifest(source_path, exported)
    assert await import_parity(engine, source_path) == len(records)
    async with engine.connect() as connection:
        assert await connection.scalar(select(work_event_handles.c.id).where(
            work_event_handles.c.work_id == TOMBSTONE
        )) == TOMBSTONE_EVENT
    write_manifest(target_path, await target_parity(engine))
    assert compare_parity_exports(source_path, target_path).records == len(records)


async def test_zero_membership_exports_only_identity_tombstone(
    engine: AsyncEngine, tmp_path: Path,
):
    async with engine.begin() as connection:
        await connection.execute(insert(work_handles).values(
            id=TOMBSTONE, provider="asana", provider_work_id="retired",
        ))
        await connection.execute(insert(work_event_handles).values(
            id=TOMBSTONE_EVENT, work_id=TOMBSTONE, provider="asana",
            provider_work_id="retired", provider_event_id="retired-story",
        ))

    class WithZeroMembership(Source):
        zero_membership = True
        retired_parent = False
        current_zero_membership = False

        async def get(self, provider_work_id: str) -> ProviderWork | None:
            if provider_work_id == "retired":
                raise ProviderWorkDecodeError("priority_truth")
            return await super().get(provider_work_id)

        async def has_zero_memberships(self, provider_work_id: str) -> bool:
            return provider_work_id == "retired" and self.zero_membership

        async def snapshot_for_import(self, provider_work_id: str):
            if provider_work_id == "retired":
                raise AssertionError("retired content must not be read")
            snapshot = await super().snapshot_for_import(provider_work_id)
            if provider_work_id == "parent" and self.current_zero_membership:
                assert snapshot is not None
                return snapshot[0], snapshot[1], snapshot[2], True
            if provider_work_id == "child" and self.retired_parent:
                assert snapshot is not None
                return snapshot[0], "retired", snapshot[2], snapshot[3]
            return snapshot

        async def source_stories(self, provider_task_id: str, *args, **kwargs):
            if provider_task_id == "retired":
                raise AssertionError("retired history must not be read")
            return await super().source_stories(provider_task_id, *args, **kwargs)

    provider = WithZeroMembership()
    corpus_path = tmp_path / "corpus.json"
    await corpus(engine, provider, corpus_path)

    ignored_counts: list[int] = []
    exported = await source_parity(
        engine, provider, corpus_path, progress=ignored_counts.append,
    )
    assert ignored_counts == [1]
    records = cast(list[dict[str, object]], exported["records"])
    retired = [row for row in records if row["id"] in {
        f'["{TOMBSTONE}"]', '["retired"]', f'["{TOMBSTONE_EVENT}"]',
    }]
    assert retired == []

    provider.zero_membership = False
    with pytest.raises(ValueError, match="gained a membership"):
        await source_parity(engine, provider, corpus_path)
    provider.zero_membership = True

    provider.retired_parent = True
    with pytest.raises(ValueError, match="parent is retired"):
        await source_parity(engine, provider, corpus_path)
    provider.retired_parent = False

    provider.current_zero_membership = True
    with pytest.raises(ValueError, match="retired after corpus capture"):
        await source_parity(engine, provider, corpus_path)
    provider.current_zero_membership = False

    provider.dependencies["child"] = frozenset(("parent", "retired"))
    dependency_corpus = tmp_path / "dependency-corpus.json"
    await corpus(engine, provider, dependency_corpus)
    with pytest.raises(ValueError, match="dependency is retired"):
        await source_parity(engine, provider, dependency_corpus)
    provider.dependencies["child"] = frozenset(("parent",))

    source_path, target_path = tmp_path / "source.json", tmp_path / "target.json"
    write_manifest(source_path, exported)
    wrong_source = tmp_path / "wrong-retained-source.json"
    write_manifest(wrong_source, parity_manifest([*records, {
        "kind": "work", "id": f'["{TOMBSTONE}"]', "fields": {
            "work_id": str(TOMBSTONE), "title": "Wrong retained content",
            "normalized_title": "wrong retained content", "completed": True,
            "notes": "must not migrate", "assignee": None, "priority": None,
            "work_type": None, "lifecycle_state": None,
            "review_next_action": None, "wait_kind": None,
            "unblock_condition": None, "next_due": None, "row_version": 1,
        },
    }, {
        "kind": "alias", "id": '["retired"]', "fields": {
            "asana_task_gid": "retired", "work_id": str(TOMBSTONE),
        },
    }]))
    with pytest.raises(ValueError, match="retired identities do not match"):
        await import_parity(engine, wrong_source, corpus_path)
    async with engine.connect() as connection:
        assert await connection.scalar(select(work_event_handles.c.id).where(
            work_event_handles.c.work_id == TOMBSTONE
        )) == TOMBSTONE_EVENT
        assert await connection.scalar(select(canonical_work.c.work_id)) is None
    with pytest.raises(ValueError, match="no source corpus"):
        await import_parity(engine, source_path)
    async with engine.connect() as connection:
        assert await connection.scalar(select(work_event_handles.c.id).where(
            work_event_handles.c.work_id == TOMBSTONE
        )) == TOMBSTONE_EVENT
    assert await import_parity(engine, source_path, corpus_path) == len(records)
    async with engine.connect() as connection:
        assert await connection.scalar(
            work_handles.select().with_only_columns(work_handles.c.id).where(
                work_handles.c.provider_work_id == "retired"
            )
        ) == TOMBSTONE
        assert await connection.scalar(select(work_event_handles.c.id).where(
            work_event_handles.c.work_id == TOMBSTONE
        )) is None
    write_manifest(target_path, await target_parity(engine))
    assert compare_parity_exports(source_path, target_path).records == len(records)


async def test_tombstone_disposition_is_exact_and_cannot_hide_other_exceptions(
    engine: AsyncEngine, tmp_path: Path,
):
    async with engine.begin() as connection:
        await connection.execute(insert(work_handles).values(
            id=TOMBSTONE, provider="asana", provider_work_id="retired",
        ))

    class WithMissingTombstone(Source):
        async def get(self, provider_work_id: str) -> ProviderWork | None:
            return None if provider_work_id == "retired" else await super().get(provider_work_id)

    provider = WithMissingTombstone()
    corpus_path = tmp_path / "corpus.json"
    await corpus(engine, provider, corpus_path)
    source_corpus = json.loads(corpus_path.read_text())
    with pytest.raises(ValueError, match="require a reviewed"):
        await source_parity(engine, provider, corpus_path)

    disposition_path = tmp_path / "tombstones.json"
    write_manifest(disposition_path, digested({
        "schema_version": 1,
        "corpus_sha256": source_corpus["sha256"],
        "tombstones": [{
            "provider_work_id": "retired", "work_id": str(TOMBSTONE),
            "source_reason": "decode:priority_truth",
            "disposition": "retired_tombstone", "title": "Wrong decision",
            "notes": "", "event_aliases": [],
        }],
    }))
    with pytest.raises(ValueError, match="does not match a retired"):
        await source_parity(engine, provider, corpus_path, disposition_path)


async def test_tombstone_disposition_binds_every_known_event_alias(
    engine: AsyncEngine, tmp_path: Path,
):
    async with engine.begin() as connection:
        await connection.execute(insert(work_handles).values(
            id=TOMBSTONE, provider="asana", provider_work_id="retired",
        ))
        await connection.execute(insert(work_event_handles).values(
            id=TOMBSTONE_EVENT, work_id=TOMBSTONE, provider="asana",
            provider_work_id="retired", provider_event_id="known-story",
        ))

    class WithMissingTombstone(Source):
        async def get(self, provider_work_id: str) -> ProviderWork | None:
            return None if provider_work_id == "retired" else await super().get(provider_work_id)

    provider = WithMissingTombstone()
    corpus_path = tmp_path / "corpus.json"
    await corpus(engine, provider, corpus_path)
    source_corpus = json.loads(corpus_path.read_text())
    disposition_path = tmp_path / "tombstones.json"
    write_manifest(disposition_path, digested({
        "schema_version": 1, "corpus_sha256": source_corpus["sha256"],
        "tombstones": [{
            "provider_work_id": "retired", "work_id": str(TOMBSTONE),
            "source_reason": "missing", "disposition": "retired_tombstone",
            "title": "Retired", "notes": "", "event_aliases": [],
        }],
    }))
    with pytest.raises(ValueError, match="event aliases changed"):
        await source_parity(engine, provider, corpus_path, disposition_path)
