import os
import stat
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
from switchstand.work_index import ActivationNotCommitted, ActivationReceipt
from switchstand.work_index_migration import _stable_corpus, migrate, run, write_receipt
from switchstand.work_metadata_migration import run as metadata_run

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
        _row("other", None, []),
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
            "counts": {"broad": 3, "bound": 2, "included": 3, "exceptions": 1},
        }
    )


def _paths(tmp_path: Path, manifest: dict[str, object]) -> tuple[Path, Path]:
    tmp_path.mkdir(exist_ok=True)
    paths = tmp_path / "first.json", tmp_path / "second.json"
    for path in paths:
        write_manifest(path, manifest)
    return paths


async def _prepare(paths, corpus_digest, exception_digest):
    return await migrate(
        "prepare",
        confirm_offline=True,
        expected_corpus_digest=corpus_digest,
        expected_prepared_digest=None,
        receipt_path=None,
        manifest_paths=paths,
        expected_exception_digest=exception_digest,
    )


def test_receipt_is_atomically_private_under_permissive_umask(tmp_path):
    path = tmp_path / "receipt.json"
    prior = os.umask(0o022)
    try:
        write_receipt(path, ActivationReceipt(1, 1, "a" * 64))
    finally:
        os.umask(prior)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".receipt.json.*"))
    with pytest.raises(FileExistsError):
        write_receipt(path, ActivationReceipt(2, 1, "b" * 64))


def test_cli_distinguishes_proven_noncommit_from_unknown(monkeypatch, capsys):
    async def not_committed(*_args, **_kwargs):
        raise ActivationNotCommitted("proven absent")

    monkeypatch.setattr("switchstand.work_index_migration.migrate", not_committed)
    with pytest.raises(SystemExit) as stopped:
        run(["reconcile", "--confirm-offline", "--receipt", "attempt.json"])
    assert stopped.value.code == 3
    assert "NOT_COMMITTED" in capsys.readouterr().err

    async def malformed(*_args, **_kwargs):
        raise ValueError("malformed receipt")

    monkeypatch.setattr("switchstand.work_index_migration.migrate", malformed)
    with pytest.raises(SystemExit) as stopped:
        run(["reconcile", "--confirm-offline", "--receipt", "attempt.json"])
    assert stopped.value.code == 2
    assert "UNKNOWN" in capsys.readouterr().err


def test_stage2_cli_distinguishes_proven_noncommit_from_unknown(monkeypatch, capsys):
    async def fail(*_args, **_kwargs):
        raise ActivationNotCommitted("proven absent")

    monkeypatch.setattr("switchstand.work_metadata_migration.execute", fail)
    args = ["reconcile", "worksheet.json", "--confirm-offline", "--receipt", "attempt.json"]
    with pytest.raises(SystemExit) as stopped:
        metadata_run(args)
    assert stopped.value.code == 3
    assert "NOT_COMMITTED" in capsys.readouterr().err

    async def unknown(*_args, **_kwargs):
        raise OSError("unreadable receipt")

    monkeypatch.setattr("switchstand.work_metadata_migration.execute", unknown)
    with pytest.raises(SystemExit) as stopped:
        metadata_run(args)
    assert stopped.value.code == 2
    assert "UNKNOWN" in capsys.readouterr().err


def test_frozen_corpus_requires_matching_scans_reviewed_exceptions_and_closed_dependencies(
    tmp_path,
):
    manifest = _manifest()
    corpus_digest = str(manifest["sha256"])
    paths = _paths(tmp_path, manifest)
    exception_digest = manifest_exception_digest(manifest)

    with pytest.raises(ValueError, match="reviewed digest"):
        _stable_corpus(paths, corpus_digest, "0" * 64)

    replacement = dict(manifest)
    replacement["source_candidate"] = "b" * 40
    replacement.pop("sha256")
    replacement = _with_digest(replacement)
    replacement_paths = _paths(tmp_path / "replacement", replacement)
    with pytest.raises(ValueError, match="manifest digest"):
        _stable_corpus(replacement_paths, corpus_digest, exception_digest)

    changed = _manifest(external_dependency=True)
    changed_path = tmp_path / "changed.json"
    write_manifest(changed_path, changed)
    with pytest.raises(ValueError, match="do not match"):
        _stable_corpus((paths[0], changed_path), corpus_digest, exception_digest)
    with pytest.raises(ValueError, match="inside the included corpus"):
        _stable_corpus(
            (changed_path, changed_path),
            str(changed["sha256"]),
            manifest_exception_digest(changed),
        )


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
                "(:mailbox, 'agent-mailbox', 'Coordinator'), "
                "('40000000-0000-4000-8000-000000000004', 'asana', 'new')"
            ),
            {"known": KNOWN, "exception": EXCEPTION, "mailbox": MAILBOX},
        )
    manifest = _manifest()
    paths = _paths(tmp_path, manifest)
    corpus_digest = str(manifest["sha256"])
    exception_digest = manifest_exception_digest(manifest)

    await stage1_engine.dispose()
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    with pytest.raises(ValueError, match="bindings changed"):
        await _prepare(paths, corpus_digest, exception_digest)
    async with stage1_engine.begin() as connection:
        await connection.execute(text("DELETE FROM work_handles WHERE provider_work_id = 'new'"))
    await stage1_engine.dispose()
    digest = await _prepare(paths, corpus_digest, exception_digest)
    assert isinstance(digest, str)
    async with stage1_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO work_handles (id, provider, provider_work_id) "
                "VALUES ('40000000-0000-4000-8000-000000000004', 'asana', 'drift')"
            )
        )
    await stage1_engine.dispose()
    with pytest.raises(ValueError, match="bindings changed"):
        await _prepare(paths, corpus_digest, exception_digest)
    async with stage1_engine.begin() as connection:
        await connection.execute(text("DELETE FROM work_handles WHERE provider_work_id = 'drift'"))
        assert await connection.scalar(text("SELECT count(*) FROM work_authority")) == 0
    await stage1_engine.dispose()
    retry_digest = await _prepare(paths, corpus_digest, exception_digest)
    assert retry_digest == digest
    receipt = await migrate(
        "activate",
        confirm_offline=True,
        expected_corpus_digest=corpus_digest,
        expected_prepared_digest=digest,
        receipt_path=tmp_path / "receipt.json",
        manifest_paths=paths,
        expected_exception_digest=exception_digest,
    )
    assert isinstance(receipt, ActivationReceipt) and receipt.count == 3
    async with stage1_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM work_index")) == 3
        assert await connection.scalar(text("SELECT count(*) FROM work_handles")) == 5
        retained = await connection.scalar(
            text("SELECT count(*) FROM work_handles WHERE id IN (:exception, :mailbox)"),
            {"exception": EXCEPTION, "mailbox": MAILBOX},
        )
    assert retained == 2
