import os
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.contracts import Routing, WorkContext
from switchstand.work_corpus import _with_digest, manifest_exception_digest, write_manifest
from switchstand.work_index import ActivationReceipt
from switchstand.work_index_migration import _stable_corpus, _validate_bindings, migrate

KNOWN = UUID("10000000-0000-4000-8000-000000000001")
EXCEPTION = UUID("20000000-0000-4000-8000-000000000002")
MAILBOX = UUID("30000000-0000-4000-8000-000000000003")


@pytest.fixture
async def stage1_engine(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for migration tests")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await engine.dispose()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


def _row(provider_id: str, work_id: UUID | None, dependencies: list[str]) -> dict[str, object]:
    return {
        "provider_work_id": provider_id,
        "work_id": str(work_id) if work_id else None,
        "title": f"Work {provider_id}",
        "completed": False,
        "revision": f"revision-{provider_id}",
        "routing": Routing(priority="P0").model_dump(mode="json"),
        "context": WorkContext(assignee="Marco").model_dump(mode="json"),
        "dependencies": dependencies,
    }


def _manifest(*, external_dependency: bool = False) -> dict[str, object]:
    rows = [
        _row("known", KNOWN, ["missing" if external_dependency else "new"]),
        _row("new", None, []),
    ]
    return _with_digest(
        {
            "schema_version": 1,
            "source_candidate": "a" * 40,
            "rows": rows,
            "exceptions": [
                {
                    "provider_work_id": "gone",
                    "work_id": str(EXCEPTION),
                    "reason": "missing",
                }
            ],
            "counts": {"broad": 2, "bound": 2, "included": 2, "exceptions": 1},
        }
    )


def _paths(tmp_path: Path, manifest: dict[str, object]) -> tuple[Path, Path]:
    paths = tmp_path / "first.json", tmp_path / "second.json"
    for path in paths:
        write_manifest(path, manifest)
    return paths


def test_frozen_corpus_requires_matching_scans_reviewed_exceptions_and_closed_dependencies(
    tmp_path,
):
    manifest = _manifest()
    paths = _paths(tmp_path, manifest)
    corpus = _stable_corpus(paths, manifest_exception_digest(manifest))
    assert set(corpus.work_ids) == {"known", "new"}

    with pytest.raises(ValueError, match="reviewed digest"):
        _stable_corpus(paths, "0" * 64)

    changed = _manifest(external_dependency=True)
    changed_path = tmp_path / "changed.json"
    write_manifest(changed_path, changed)
    with pytest.raises(ValueError, match="do not match"):
        _stable_corpus((paths[0], changed_path), manifest_exception_digest(manifest))
    with pytest.raises(ValueError, match="inside the included corpus"):
        _stable_corpus((changed_path, changed_path), manifest_exception_digest(changed))


async def test_frozen_corpus_prepares_exact_bindings_without_replacing_other_state(
    stage1_engine: AsyncEngine,
    monkeypatch,
    tmp_path,
):
    async with stage1_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO work_handles (id, provider, provider_work_id) VALUES "
                "(:known, 'asana', 'known'), (:exception, 'asana', 'gone'), "
                "(:mailbox, 'agent-mailbox', 'Coordinator')"
            ),
            {"known": KNOWN, "exception": EXCEPTION, "mailbox": MAILBOX},
        )
    manifest = _manifest()
    paths = _paths(tmp_path, manifest)
    exception_digest = manifest_exception_digest(manifest)
    corpus = _stable_corpus(paths, exception_digest)
    await _validate_bindings(stage1_engine, corpus, prepared=False)

    async with stage1_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO work_handles (id, provider, provider_work_id) "
                "VALUES ('40000000-0000-4000-8000-000000000004', 'asana', 'drift')"
            )
        )
    with pytest.raises(ValueError, match="bindings changed"):
        await _validate_bindings(stage1_engine, corpus, prepared=False)
    async with stage1_engine.begin() as connection:
        await connection.execute(text("DELETE FROM work_handles WHERE provider_work_id = 'drift'"))
        assert await connection.scalar(text("SELECT count(*) FROM work_authority")) == 0

    await stage1_engine.dispose()
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    digest = await migrate(
        "prepare",
        confirm_offline=True,
        expected_manifest_digest=None,
        receipt_path=None,
        manifest_paths=paths,
        expected_exception_digest=exception_digest,
    )
    assert isinstance(digest, str)
    await _validate_bindings(stage1_engine, corpus, prepared=True)
    await stage1_engine.dispose()
    receipt = await migrate(
        "activate",
        confirm_offline=True,
        expected_manifest_digest=digest,
        receipt_path=tmp_path / "receipt.json",
        manifest_paths=paths,
        expected_exception_digest=exception_digest,
    )
    assert isinstance(receipt, ActivationReceipt) and receipt.count == 2
    async with stage1_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM work_index")) == 2
        assert await connection.scalar(text("SELECT count(*) FROM work_handles")) == 4
        retained = (
            await connection.execute(
                text(
                    "SELECT provider, provider_work_id FROM work_handles "
                    "WHERE id IN (:exception, :mailbox) ORDER BY provider"
                ),
                {"exception": EXCEPTION, "mailbox": MAILBOX},
            )
        ).all()
    assert retained == [("agent-mailbox", "Coordinator"), ("asana", "gone")]
