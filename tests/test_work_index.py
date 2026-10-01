import os
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from chatgpt_fixture import PRINCIPAL, grant
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.chatgpt import ChatGPTService
from switchstand.contracts import Routing, WorkContext, WorkPlacement, WorkSearchRequest
from switchstand.core import Handle, ProviderWork
from switchstand.discovery import ProviderSearchItem, ProviderSearchPage
from switchstand.grant_state import GrantState
from switchstand.state import PostgresState, metadata
from switchstand.work_index import WorkIndex, activate, normalize_title
from switchstand.work_index_migration import final_scan


def item(gid: str, title: str, *, completed: bool = False) -> ProviderSearchItem:
    return ProviderSearchItem(
        provider_work_id=gid, title=title, completed=completed,
        revision=f"revision-{gid}", routing=Routing(priority="P0"),
        context=WorkContext(assignee="Marco"),
    )


@pytest.mark.parametrize("title", ["before\0after", "\ud800"])
def test_title_normalization_rejects_postgres_unsupported_text(title):
    with pytest.raises(ValueError):
        normalize_title(title)


@pytest.fixture
async def index(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for work-index tests")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    await engine.dispose()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_async_engine(url)
    yield WorkIndex(engine)
    async with engine.begin() as connection:
        await connection.execute(text(
            "TRUNCATE work_authority_cutovers, work_authority, work_index CASCADE"
        ))
    await engine.dispose()


async def test_atomic_cutover_reuses_handles_and_excludes_synthetic_or_foreign(index):
    known = Handle(UUID("10000000-0000-4000-8000-000000000001"), "asana", "1")
    synthetic = Handle(UUID("20000000-0000-4000-8000-000000000002"), "agent-mailbox", "mail")
    foreign = Handle(UUID("30000000-0000-4000-8000-000000000003"), "asana", "foreign")
    async with index.engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO work_handles (id, provider, provider_work_id) VALUES "
            "(:known, 'asana', '1'), (:synthetic, 'agent-mailbox', 'mail'), "
            "(:foreign, 'asana', 'foreign')"
        ), {"known": known.id, "synthetic": synthetic.id, "foreign": foreign.id})

    assert await activate(index.engine, (item("1", "Alpha"), item("2", "Beta", completed=True))) == 2
    assert (await index.get(known.id)).title == "Alpha"  # type: ignore[union-attr]
    assert await index.get(synthetic.id) is None
    assert await index.get(foreign.id) is None
    async with index.engine.connect() as connection:
        marker = await connection.scalar(text("SELECT count(*) FROM work_authority_cutovers"))
    assert marker == 1


async def test_db_search_filter_pagination_and_cold_reader_need_no_provider(index):
    await activate(index.engine, (
        item("1", "Alpha task"), item("2", "Beta task"),
        item("3", "Gamma", completed=True),
    ))
    first = await index.search(WorkSearchRequest(api_version="1", limit=1))
    assert first is not None and [row.title for row in first.items] == ["Alpha task"]
    assert first.next_cursor is not None
    second = await WorkIndex(index.engine).search(WorkSearchRequest(
        api_version="1", limit=1, cursor=first.next_cursor,
    ))
    assert second is not None and [row.title for row in second.items] == ["Beta task"]
    found = await index.search(WorkSearchRequest(api_version="1", text="task"))
    assert found is not None and [row.title for row in found.items] == ["Alpha task", "Beta task"]
    completed = await index.search(WorkSearchRequest(api_version="1", completed=True))
    assert completed is not None and [row.title for row in completed.items] == ["Gamma"]
    with pytest.raises(ValueError, match="cursor"):
        await index.search(WorkSearchRequest(api_version="1", completed=True,
                                             cursor=first.next_cursor))


async def test_service_search_is_db_only_when_provider_search_is_unavailable(index):
    await activate(index.engine, (item("1", "Offline searchable"),))
    async with index.engine.connect() as connection:
        work_id = await connection.scalar(text("SELECT work_id FROM work_index"))
    principal = PRINCIPAL.model_copy(update={"subject": str(UUID(int=42))})
    selected = grant(
        principal=principal, active=work_id, scope="workspace",
        operations=frozenset({"work_search"}),
    )
    grants = GrantState(index.engine)
    await grants.issue(selected, None)

    class Unavailable:
        async def search_work(self, *_args):
            raise AssertionError("post-cutover search must not call the provider")

    async def resolve():
        return principal

    service = ChatGPTService(
        resolve, PostgresState(index.engine), grants, {"asana": Unavailable()}
    )
    result = await service.search(WorkSearchRequest(api_version="1", text="searchable"))
    assert result.status == "ok" and [row.id for row in result.items] == [work_id]


async def test_cutover_marker_without_active_row_fails_closed(index):
    await activate(index.engine, (item("1", "Never restore provider authority"),))
    async with index.engine.connect() as connection:
        work_id = await connection.scalar(text("SELECT work_id FROM work_index"))
    async with index.engine.begin() as connection:
        await connection.execute(text("DELETE FROM work_authority"))
    with pytest.raises(ValueError, match="inconsistent irreversible"):
        await index.generation()

    principal = PRINCIPAL.model_copy(update={"subject": str(UUID(int=43))})
    selected = grant(
        principal=principal, active=work_id, scope="workspace",
        operations=frozenset({"work_search"}),
    )
    grants = GrantState(index.engine)
    await grants.issue(selected, None)

    class ProviderMustNotRun:
        async def search_work(self, *_args):
            raise AssertionError("durable cutover marker forbids provider search fallback")

    async def resolve():
        return principal

    service = ChatGPTService(
        resolve, PostgresState(index.engine), grants, {"asana": ProviderMustNotRun()}
    )
    result = await service.search(WorkSearchRequest(api_version="1"))
    assert result.status == "unknown"


async def test_scalar_update_is_db_only_composite_and_stale_safe(index):
    await activate(index.engine, (item("1", "Provider title"),))
    async with index.engine.connect() as connection:
        work_id = await connection.scalar(text(
            "SELECT id FROM work_handles WHERE provider = 'asana' AND provider_work_id = '1'"
        ))
    assert isinstance(work_id, UUID)
    provider = ProviderWork(
        "Arbitrarily changed in Asana", "notes", False, "revision-1",
        Routing(priority="P0"), WorkContext(assignee="Marco"), True,
    )
    projected = await index.project(work_id, provider)
    assert (projected.title, projected.completed) == ("Provider title", False)
    revision, applied = await index.update_fields(
        work_id, projected.revision, provider, {"title": "DB title", "completed": True}
    )
    assert applied and revision != projected.revision
    stale_revision, applied = await index.update_fields(
        work_id, projected.revision, provider, {"title": "Lost update"}
    )
    assert not applied and stale_revision == revision
    provider = ProviderWork(
        "Still ignored", provider.notes, False, provider.revision,
        provider.routing, provider.context, provider.canonical,
    )
    final = await WorkIndex(index.engine).project(work_id, provider)
    assert (final.title, final.completed) == ("DB title", True)


async def test_copied_messy_shape_survives_offline_import(index):
    copied = ProviderSearchItem(
        provider_work_id="messy", title="  Café launch — blocked / review  ", completed=False,
        revision="2026-09-30T23:59:59.999Z",
        routing=Routing(
            priority="P-CRITICAL", work_type="Implementation",
            horizon="Stage 2", review_next_action="Needs Work", stage3_gate="Hold",
        ),
        context=WorkContext(
            assignee="Marco", placements=(
                WorkPlacement(area="Control plane", stage="WAITING"),
                WorkPlacement(area="Related migration"),
            ),
        ),
    )
    await activate(index.engine, (copied,))
    result = await index.search(WorkSearchRequest(api_version="1", text="café blocked"))
    assert result is not None and result.items[0].title == copied.title
    assert result.items[0].routing == copied.routing
    assert result.items[0].context == copied.context


async def test_failed_pre_cutover_import_leaves_no_authority(index):
    with pytest.raises(ValueError, match="exactly once"):
        await activate(index.engine, (item("same", "A"), item("same", "B")))
    assert not await index.active()
    async with index.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM work_index")) == 0
        assert await connection.scalar(text("SELECT count(*) FROM work_authority_cutovers")) == 0


async def test_populated_cutover_marker_refuses_downgrade(index, monkeypatch):
    await activate(index.engine, (item("1", "Alpha"),))
    with pytest.raises(IntegrityError):
        async with index.engine.begin() as connection:
            await connection.execute(text(
                "UPDATE work_authority SET state = 'IMPORTING' WHERE scope = 'workspace'"
            ))
    async with index.engine.begin() as connection:
        await connection.execute(text("DELETE FROM work_index"))
    url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    with pytest.raises(RuntimeError, match="POSTGRES_AUTHORITY is irreversible"):
        command.downgrade(config, "0007_agent_chat_identity")
    assert await index.active()


async def test_final_scan_rejects_duplicate_and_cursor_cycle():
    class Provider:
        async def search_work(self, _text, _completed, cursor, _limit):
            if cursor is None:
                return ProviderSearchPage((item("1", "A"),), "again")
            return ProviderSearchPage((item("1", "A"),), "again")

    with pytest.raises(ValueError, match="duplicate"):
        await final_scan(Provider())  # type: ignore[arg-type]
