import asyncio
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

from switchstand import work_index
from switchstand.contracts import Routing, WorkContext
from switchstand.work_corpus import _with_digest, manifest_exception_digest, write_manifest
from switchstand.work_index import (
    ActivationNotCommitted,
    ActivationReceipt,
    ActivationUnknown,
    PrepareReceipt,
    canonical_digest,
    cleanup_preparation,
    reconcile_preparation,
)
from switchstand.work_index_migration import (
    _stable_corpus,
    load_prepare_receipt,
    migrate,
    run,
    write_receipt,
)
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
        await connection.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ))
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
    result = await migrate(
        "prepare",
        confirm_offline=True,
        expected_corpus_digest=corpus_digest,
        expected_prepared_digest=None,
        receipt_path=paths[0].parent / "prepare.json",
        manifest_paths=paths,
        expected_exception_digest=exception_digest,
    )
    assert isinstance(result, PrepareReceipt)
    return result.prepared_digest


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


def test_prepare_receipt_rejects_noncanonical_or_unbound_identity(tmp_path):
    receipt = PrepareReceipt(
        "a" * 64, "b" * 64, "c" * 64, (),
        (("item", "10000000-0000-4000-8000-000000000001"),),
        (("item", "10000000-0000-4000-8000-000000000001"),),
    )
    path = tmp_path / "prepare.json"
    write_receipt(path, receipt)
    with pytest.raises(ValueError, match="digest is inconsistent"):
        load_prepare_receipt(path)


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
    for action in ("reconcile", "prepare-reconcile", "prepare-cleanup"):
        with pytest.raises(SystemExit) as stopped:
            run([action, "--confirm-offline", "--receipt", "attempt.json"])
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
    real_commit = work_index._commit

    async def commit_then_lose_output(transaction):
        await real_commit(transaction)
        raise OSError("simulated lost commit response")

    monkeypatch.setattr(work_index, "_commit", commit_then_lose_output)
    digest = await _prepare(paths, corpus_digest, exception_digest)
    monkeypatch.setattr(work_index, "_commit", real_commit)
    assert isinstance(digest, str)
    async with stage1_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO work_handles (id, provider, provider_work_id) "
                "VALUES ('40000000-0000-4000-8000-000000000004', 'asana', 'drift')"
            )
        )
    await stage1_engine.dispose()
    with pytest.raises(ActivationUnknown, match="bindings changed"):
        await _prepare(paths, corpus_digest, exception_digest)
    async with stage1_engine.begin() as connection:
        await connection.execute(text("DELETE FROM work_handles WHERE provider_work_id = 'drift'"))
        assert await connection.scalar(text("SELECT count(*) FROM work_authority")) == 0
    await stage1_engine.dispose()
    retry_digest = await _prepare(paths, corpus_digest, exception_digest)
    assert retry_digest == digest
    prepare_receipt = load_prepare_receipt(tmp_path / "prepare.json")
    inserted = dict(prepare_receipt.inserted_bindings)
    expected_before = (("gone", str(EXCEPTION)), ("known", str(KNOWN)))
    forged_inserted = tuple(sorted(prepare_receipt.inserted_bindings + (("known", str(KNOWN)),)))
    forged = PrepareReceipt(
        corpus_digest, digest, canonical_digest(forged_inserted),
        (("gone", str(EXCEPTION)),), prepare_receipt.final_bindings, forged_inserted,
    )
    with pytest.raises(ActivationUnknown, match="reviewed manifests"):
        await cleanup_preparation(stage1_engine, forged, prepare_receipt_items := tuple(
            _stable_corpus(paths, corpus_digest, exception_digest).items
        ), expected_before)
    async with stage1_engine.connect() as blocker:
        transaction = await blocker.begin()
        await blocker.execute(text("SELECT pg_advisory_xact_lock(1398032177)"))
        reconciliation = asyncio.create_task(reconcile_preparation(stage1_engine, prepare_receipt))
        await asyncio.sleep(0.05)
        assert not reconciliation.done()
        await blocker.execute(text(
            "INSERT INTO work_authority (scope, state, generation) "
            "VALUES ('workspace', 'POSTGRES_AUTHORITY', 1)"
        ))
        await transaction.commit()
    with pytest.raises(ActivationUnknown, match="authority boundary"):
        await reconciliation
    async with stage1_engine.begin() as connection:
        await connection.execute(text("DELETE FROM work_authority"))
    async with stage1_engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO work_handles (id, provider, provider_work_id) VALUES "
            "('40000000-0000-4000-8000-000000000004', 'asana', 'drift')"
        ))
    with pytest.raises(ActivationUnknown, match="bindings changed"):
        await cleanup_preparation(stage1_engine, prepare_receipt, prepare_receipt_items, expected_before)
    async with stage1_engine.begin() as connection:
        assert await connection.scalar(text(
            "SELECT count(*) FROM work_handles WHERE provider_work_id IN ('new', 'other')"
        )) == 2
        await connection.execute(text("DELETE FROM work_handles WHERE provider_work_id = 'drift'"))
    monkeypatch.setattr(work_index, "_commit", commit_then_lose_output)
    await cleanup_preparation(stage1_engine, prepare_receipt, prepare_receipt_items, expected_before)
    monkeypatch.setattr(work_index, "_commit", real_commit)
    await cleanup_preparation(stage1_engine, prepare_receipt, prepare_receipt_items, expected_before)
    async with stage1_engine.connect() as connection:
        rows = (await connection.execute(text(
            "SELECT provider_work_id, id FROM work_handles WHERE provider = 'asana'"
        ))).all()
    assert {row[0] for row in rows} == {"known", "gone"}
    await stage1_engine.dispose()
    assert await _prepare(paths, corpus_digest, exception_digest) == digest
    async with stage1_engine.connect() as connection:
        restored = (await connection.execute(text(
            "SELECT provider_work_id, id FROM work_handles "
            "WHERE provider_work_id IN ('new', 'other')"
        ))).all()
    assert {provider: str(identity) for provider, identity in restored} == inserted
    await stage1_engine.dispose()
    activation = await migrate(
        "activate",
        confirm_offline=True,
        expected_corpus_digest=corpus_digest,
        expected_prepared_digest=digest,
        receipt_path=tmp_path / "activation.json",
        manifest_paths=paths,
        expected_exception_digest=exception_digest,
    )
    assert isinstance(activation, ActivationReceipt) and activation.count == 3
    with pytest.raises(ActivationUnknown, match="authority exists"):
        await cleanup_preparation(stage1_engine, prepare_receipt, prepare_receipt_items, expected_before)
    async with stage1_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM work_index")) == 3
        assert await connection.scalar(text("SELECT count(*) FROM work_handles")) == 5
        retained = await connection.scalar(
            text("SELECT count(*) FROM work_handles WHERE id IN (:exception, :mailbox)"),
            {"exception": EXCEPTION, "mailbox": MAILBOX},
        )
    assert retained == 2
