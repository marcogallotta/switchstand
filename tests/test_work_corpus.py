import os
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.contracts import Routing, WorkContext
from switchstand.core import Handle, ProviderError, ProviderWork
from switchstand.discovery import ProviderSearchItem, ProviderSearchPage
from switchstand.state import metadata
from switchstand.work_corpus import (
    capture_manifest,
    compare_manifests,
    load_manifest,
    write_manifest,
)

SHA = "3a04669a9f7a5c094bd7617c55003ed47a098f80"


@pytest.fixture
async def index(database_prerequisite):
    del database_prerequisite
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for work-corpus tests")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    await engine.dispose()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_async_engine(url)
    yield type("Index", (), {"engine": engine})()
    await engine.dispose()


def search_item(gid: str, revision: str | None = None) -> ProviderSearchItem:
    return ProviderSearchItem(
        gid,
        f"Task {gid}",
        False,
        revision or f"revision-{gid}",
        Routing(priority="P1"),
        WorkContext(assignee="Marco"),
    )


def provider_work(gid: str, *, canonical: bool = True) -> ProviderWork:
    item = search_item(gid)
    return ProviderWork(
        item.title,
        f"notes-{gid}",
        item.completed,
        item.revision,
        item.routing,
        item.context,
        canonical,
    )


class FakeProvider:
    def __init__(
        self,
        pages: dict[str | None, ProviderSearchPage],
        exact: dict[str, ProviderWork | None],
        dependencies: dict[str, frozenset[str]] | None = None,
    ):
        self.pages = pages
        self.exact = exact
        self.dependencies = dependencies or {}

    async def search_work(self, _text, _completed, cursor, _limit):
        return self.pages[cursor]

    async def get(self, provider_work_id):
        return self.exact[provider_work_id]

    async def dependencies_for_import(self, provider_work_id):
        return self.dependencies.get(provider_work_id, frozenset())


async def insert_handles(engine, *handles: Handle) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO work_handles (id, provider, provider_work_id) "
                "VALUES (:id, :provider, :provider_work_id)"
            ),
            [
                {
                    "id": handle.id,
                    "provider": handle.provider,
                    "provider_work_id": handle.provider_work_id,
                }
                for handle in handles
            ],
        )


async def test_stable_union_includes_readable_bound_continuity_targets(index):
    grant = Handle(UUID(int=11), "asana", "grant-target")
    unknown = Handle(UUID(int=12), "asana", "unknown-target")
    dependency = Handle(UUID(int=13), "asana", "dependency-target")
    mailbox = Handle(UUID(int=14), "agent-mailbox", "Coordinator")
    await insert_handles(index.engine, grant, unknown, dependency, mailbox)
    provider = FakeProvider(
        {None: ProviderSearchPage((search_item("broad"),), None)},
        {
            gid: provider_work(gid)
            for gid in ("grant-target", "unknown-target", "dependency-target")
        },
        {"broad": frozenset({"dependency-target"})},
    )

    manifest = await capture_manifest(index.engine, provider, SHA)

    rows = {row["provider_work_id"]: row for row in manifest["rows"]}
    assert set(rows) == {"broad", "grant-target", "unknown-target", "dependency-target"}
    assert rows["broad"]["dependencies"] == ("dependency-target",)
    assert rows["grant-target"]["work_id"] == str(grant.id)
    assert "Coordinator" not in str(manifest)
    assert manifest["counts"] == {"broad": 1, "bound": 3, "included": 4, "exceptions": 0}


async def test_missing_and_noncanonical_bound_work_are_explicit_exceptions(index):
    missing = Handle(UUID(int=21), "asana", "missing")
    moved = Handle(UUID(int=22), "asana", "moved")
    await insert_handles(index.engine, missing, moved)
    provider = FakeProvider(
        {None: ProviderSearchPage((search_item("broad"),), None)},
        {"missing": None, "moved": provider_work("moved", canonical=False)},
    )

    manifest = await capture_manifest(index.engine, provider, SHA)

    assert manifest["exceptions"] == [
        {"provider_work_id": "missing", "work_id": str(missing.id), "reason": "missing"},
        {"provider_work_id": "moved", "work_id": str(moved.id), "reason": "noncanonical"},
    ]


async def test_pagination_rejects_duplicate_and_repeated_cursor(index):
    duplicate = FakeProvider(
        {
            None: ProviderSearchPage((search_item("1"),), "next"),
            "next": ProviderSearchPage((search_item("1"),), None),
        },
        {},
    )
    with pytest.raises(ValueError, match="duplicate"):
        await capture_manifest(index.engine, duplicate, SHA)

    repeated = FakeProvider(
        {
            None: ProviderSearchPage((search_item("1"),), "next"),
            "next": ProviderSearchPage((search_item("2"),), "next"),
        },
        {},
    )
    with pytest.raises(ValueError, match="cursor"):
        await capture_manifest(index.engine, repeated, SHA)


async def test_provider_failure_aborts_instead_of_becoming_exception(index):
    bound = Handle(UUID(int=31), "asana", "failed")
    await insert_handles(index.engine, bound)

    class Failed(FakeProvider):
        async def get(self, _provider_work_id):
            raise ProviderError("down")

    provider = Failed({None: ProviderSearchPage((), None)}, {})
    with pytest.raises(ProviderError):
        await capture_manifest(index.engine, provider, SHA)


async def test_manifest_is_deterministic_create_new_and_revision_sensitive(index, tmp_path: Path):
    provider = FakeProvider(
        {
            None: ProviderSearchPage((search_item("2"), search_item("1")), None),
        },
        {},
    )
    first = await capture_manifest(index.engine, provider, SHA)
    second = await capture_manifest(index.engine, provider, SHA)
    assert first == second
    assert [row["provider_work_id"] for row in first["rows"]] == ["1", "2"]

    first_path, second_path = tmp_path / "first.json", tmp_path / "second.json"
    write_manifest(first_path, first)
    write_manifest(second_path, second)
    assert first_path.stat().st_mode & 0o777 == 0o600
    assert compare_manifests(first_path, second_path) == first["sha256"]
    assert load_manifest(first_path) == first
    with pytest.raises(FileExistsError):
        write_manifest(first_path, first)

    changed = await capture_manifest(
        index.engine,
        FakeProvider(
            {
                None: ProviderSearchPage((search_item("1", "changed"), search_item("2")), None),
            },
            {},
        ),
        SHA,
    )
    changed_path = tmp_path / "changed.json"
    write_manifest(changed_path, changed)
    with pytest.raises(ValueError, match="do not match"):
        compare_manifests(first_path, changed_path)


def test_manifest_rejects_tampering(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text('{"schema_version":1,"sha256":"bad"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        load_manifest(path)
