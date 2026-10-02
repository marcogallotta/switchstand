import os
import stat
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

import switchstand.worksets as worksets_module
from switchstand.state import (
    work_authority,
    work_authority_cutovers,
    work_handles,
    work_index,
    work_metadata_authority,
    work_metadata_cutovers,
    workset_authority,
    workset_cutovers,
    workset_memberships,
    worksets,
)
from switchstand.work_index import ActivationNotCommitted, ActivationReceipt, ActivationUnknown
from switchstand.work_index_migration import load_receipt
from switchstand.workset_capture import (
    ProviderPlacement,
    ProviderProject,
    ProviderStructureCapture,
    ProviderTaskStructure,
)
from switchstand.workset_migration import execute
from switchstand.workset_worksheet import WorksetWorksheet
from switchstand.worksets import (
    Membership,
    Workset,
    WorksetSnapshot,
    activate,
    reconcile_activation,
)

FIRST = UUID("10000000-0000-4000-8000-000000000001")
SECOND = UUID("20000000-0000-4000-8000-000000000002")
WORKSET = UUID("30000000-0000-4000-8000-000000000003")


def database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL is required")
    return value


@pytest.fixture(autouse=True)
def clean_stage3(database_prerequisite, monkeypatch: pytest.MonkeyPatch):
    async def proven_offline(_engine):
        return None

    monkeypatch.setattr("switchstand.workset_migration.require_offline", proven_offline)
    engine = create_engine(database_url())
    statement = text(
        "TRUNCATE work_parent_edges, workset_memberships, worksets, workset_cutovers, "
        "workset_authority, work_metadata_cutovers, work_metadata_authority, work_edges, "
        "work_authority_cutovers, work_authority, work_index, work_handles CASCADE"
    )
    with engine.begin() as connection:
        connection.execute(statement)
    yield
    with engine.begin() as connection:
        connection.execute(statement)
    engine.dispose()


def reviewed_worksheet(
    tmp_path: Path, *, complete: bool = True, member_role: str = "MEMBER",
) -> tuple[Path, str]:
    snapshot = WorksetSnapshot(
        (Workset(WORKSET, "project.main", "Main", "PROJECT"),),
        tuple(Membership(WORKSET, work_id, "AUTHORITATIVE", member_role) for work_id in (
            (FIRST, SECOND) if complete else (FIRST,)
        )),
        (),
    )
    capture = ProviderStructureCapture(
        "a" * 40,
        (ProviderProject("project", "Main", "p1", False, ()),),
        tuple(ProviderTaskStructure(
            f"task-{position}", "r1", None, (ProviderPlacement("project"),),
        ) for position in range(2)),
    )
    worksheet = WorksetWorksheet("c" * 64, "e" * 64, "p" * 64, capture, snapshot, ())
    path = tmp_path / "stage3-worksheet.json"
    path.write_bytes(worksheet.bytes())
    path.chmod(0o600)
    return path, worksheet.digest()


async def prepare_prior_authority(*, stage1: bool = True, stage2: bool = True) -> None:
    engine = create_async_engine(database_url())
    async with engine.begin() as connection:
        for position, work_id in enumerate((FIRST, SECOND)):
            await connection.execute(insert(work_handles).values(
                id=work_id, provider="asana", provider_work_id=f"task-{position}",
            ))
            await connection.execute(insert(work_index).values(
                work_id=work_id, title=f"Task {position}", normalized_title=f"task {position}",
                completed=False, provider_revision="r1", row_version=1, routing={}, context={},
            ))
        if stage1:
            await connection.execute(insert(work_authority).values(
                scope="workspace", state="POSTGRES_AUTHORITY", generation=1,
            ))
            await connection.execute(insert(work_authority_cutovers).values(
                scope="workspace", generation=1,
            ))
        if stage2:
            await connection.execute(insert(work_metadata_authority).values(
                scope="workspace", state="POSTGRES_AUTHORITY", generation=1,
            ))
            await connection.execute(insert(work_metadata_cutovers).values(
                scope="workspace", generation=1,
            ))
    await engine.dispose()


async def test_stage_rejects_wrong_review_digest_before_writing(tmp_path: Path):
    await prepare_prior_authority()
    path, _digest = reviewed_worksheet(tmp_path)
    with pytest.raises(ValueError, match="Human-Reviewed digest"):
        await execute("stage", path, expected_worksheet_digest="0" * 64, confirm_offline=True)
    engine = create_async_engine(database_url())
    async with engine.connect() as connection:
        assert await connection.scalar(select(text("count(*)")).select_from(worksets)) == 0
    await engine.dispose()


@pytest.mark.parametrize(
    "complete,member_role,error",
    [(False, "MEMBER", ValueError), (True, "INVALID", IntegrityError)],
)
async def test_partial_or_invalid_corpus_stage_is_atomic(
    tmp_path: Path, complete: bool, member_role: str, error: type[Exception],
):
    await prepare_prior_authority()
    path, digest = reviewed_worksheet(tmp_path, complete=complete, member_role=member_role)
    with pytest.raises(error):
        await execute("stage", path, expected_worksheet_digest=digest, confirm_offline=True)
    engine = create_async_engine(database_url())
    async with engine.connect() as connection:
        assert await connection.scalar(select(text("count(*)")).select_from(worksets)) == 0
        assert await connection.scalar(
            select(text("count(*)")).select_from(workset_memberships)
        ) == 0
    await engine.dispose()


async def test_stage_is_idempotent_and_reconcile_and_reset_bind_exact_snapshot(tmp_path: Path):
    await prepare_prior_authority()
    path, digest = reviewed_worksheet(tmp_path)
    staged = await execute("stage", path, expected_worksheet_digest=digest, confirm_offline=True)
    engine = create_async_engine(database_url())
    async with engine.connect() as connection:
        before = await connection.scalar(select(text("xmin::text")).select_from(worksets))
    assert await execute(
        "stage", path, expected_worksheet_digest=digest, confirm_offline=True,
    ) == staged
    assert await execute(
        "reconcile", path, expected_worksheet_digest=digest, confirm_offline=True,
    ) == staged
    async with engine.connect() as connection:
        assert await connection.scalar(select(text("xmin::text")).select_from(worksets)) == before
    await execute("reset", path, expected_worksheet_digest=digest, confirm_offline=True)
    async with engine.connect() as connection:
        assert await connection.scalar(select(text("count(*)")).select_from(worksets)) == 0
    await engine.dispose()


async def test_reset_refuses_after_any_authority_marker(tmp_path: Path):
    await prepare_prior_authority()
    path, digest = reviewed_worksheet(tmp_path)
    await execute("stage", path, expected_worksheet_digest=digest, confirm_offline=True)
    engine = create_async_engine(database_url())
    async with engine.begin() as connection:
        await connection.execute(insert(workset_authority).values(
            scope="workspace", state="POSTGRES_AUTHORITY", generation=1,
        ))
    with pytest.raises(ActivationUnknown, match="reset is forbidden"):
        await execute("reset", path, expected_worksheet_digest=digest, confirm_offline=True)
    async with engine.connect() as connection:
        assert await connection.scalar(select(text("count(*)")).select_from(worksets)) == 1
    await engine.dispose()


@pytest.mark.parametrize("stage1,stage2,missing", [(False, True, "Stage 1"), (True, False, "Stage 2")])
async def test_stage_requires_both_prior_authorities(
    tmp_path: Path, stage1: bool, stage2: bool, missing: str,
):
    await prepare_prior_authority(stage1=stage1, stage2=stage2)
    path, digest = reviewed_worksheet(tmp_path)
    with pytest.raises(ValueError, match=missing):
        await execute("stage", path, expected_worksheet_digest=digest, confirm_offline=True)


async def test_activate_publishes_private_receipt_and_reconciles_idempotently(tmp_path: Path):
    await prepare_prior_authority()
    path, digest = reviewed_worksheet(tmp_path)
    staged = await execute("stage", path, expected_worksheet_digest=digest, confirm_offline=True)
    receipt_path = tmp_path / "stage3-attempt.json"

    result = await execute(
        "activate", path, expected_worksheet_digest=digest, confirm_offline=True,
        receipt_path=receipt_path,
    )

    assert isinstance(result, ActivationReceipt)
    assert (result.count, result.generation, result.corpus_digest) == (2, 1, staged)
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    receipt = load_receipt(receipt_path)
    recovered = await execute(
        "reconcile-activation", path, expected_worksheet_digest=digest,
        confirm_offline=True, receipt_path=receipt_path,
    )
    assert isinstance(recovered, ActivationReceipt)
    assert recovered.recovered_after_commit_error
    assert recovered.corpus_digest == receipt.corpus_digest
    engine = create_async_engine(database_url())
    assert (await reconcile_activation(engine, receipt)).recovered_after_commit_error
    async with engine.connect() as connection:
        assert (await connection.execute(select(
            workset_authority.c.generation,
        ))).scalars().all() == [1]
        assert (await connection.execute(select(
            workset_cutovers.c.generation,
        ))).scalars().all() == [1]
    async with engine.begin() as connection:
        await connection.execute(delete(work_metadata_cutovers))
    with pytest.raises(ActivationUnknown, match="repair forward"):
        await execute(
            "reconcile-activation", path, expected_worksheet_digest=digest,
            confirm_offline=True, receipt_path=receipt_path,
        )
    await engine.dispose()


async def test_activate_recovers_lost_commit_response_without_second_flip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    await prepare_prior_authority()
    path, digest = reviewed_worksheet(tmp_path)
    await execute("stage", path, expected_worksheet_digest=digest, confirm_offline=True)
    receipt_path = tmp_path / "lost-response.json"

    async def commit_then_lose(transaction):
        await transaction.commit()
        raise OSError("lost commit response")

    monkeypatch.setattr("switchstand.worksets._commit", commit_then_lose)
    result = await execute(
        "activate", path, expected_worksheet_digest=digest, confirm_offline=True,
        receipt_path=receipt_path,
    )
    assert isinstance(result, ActivationReceipt)
    assert result.recovered_after_commit_error
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    engine = create_async_engine(database_url())
    receipt = load_receipt(receipt_path)
    assert receipt.corpus_digest == result.corpus_digest
    assert (await reconcile_activation(engine, receipt)).recovered_after_commit_error
    await engine.dispose()


async def test_successful_commit_with_failed_readback_is_unknown_and_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    await prepare_prior_authority()
    path, digest = reviewed_worksheet(tmp_path)
    await execute("stage", path, expected_worksheet_digest=digest, confirm_offline=True)
    receipt_path = tmp_path / "failed-readback.json"
    original, calls = worksets_module._activation_state, 0

    async def fail_final_readback(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("database restarted after commit")
        return await original(*args, **kwargs)

    monkeypatch.setattr(worksets_module, "_activation_state", fail_final_readback)
    with pytest.raises(ActivationUnknown, match="reconcile the durable receipt"):
        await execute(
            "activate", path, expected_worksheet_digest=digest, confirm_offline=True,
            receipt_path=receipt_path,
        )
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    monkeypatch.setattr(worksets_module, "_activation_state", original)
    recovered = await execute(
        "reconcile-activation", path, expected_worksheet_digest=digest,
        confirm_offline=True, receipt_path=receipt_path,
    )
    assert isinstance(recovered, ActivationReceipt) and recovered.recovered_after_commit_error


async def test_activation_rejects_incomplete_prerequisite_and_corpus(tmp_path: Path):
    await prepare_prior_authority()
    path, digest = reviewed_worksheet(tmp_path)
    staged = await execute("stage", path, expected_worksheet_digest=digest, confirm_offline=True)
    engine = create_async_engine(database_url())
    async with engine.begin() as connection:
        await connection.execute(delete(work_metadata_cutovers))
    with pytest.raises(ValueError, match="Stage 2 paired authority"):
        await activate(engine, staged)
    async with engine.begin() as connection:
        await connection.execute(insert(work_metadata_cutovers).values(
            scope="workspace", generation=1,
        ))
        await connection.execute(delete(workset_memberships).where(
            workset_memberships.c.work_id == SECOND,
        ))
    with pytest.raises(ValueError, match="exactly cover"):
        await activate(engine, staged)
    await engine.dispose()


async def test_reconcile_classifies_absent_corrupt_and_drifted_state(tmp_path: Path):
    await prepare_prior_authority()
    path, digest = reviewed_worksheet(tmp_path)
    staged = await execute("stage", path, expected_worksheet_digest=digest, confirm_offline=True)
    receipt = ActivationReceipt(2, 1, staged, pre_corpus_digest=staged)
    engine = create_async_engine(database_url())
    with pytest.raises(ActivationNotCommitted):
        await reconcile_activation(engine, receipt)
    async with engine.begin() as connection:
        await connection.execute(insert(workset_authority).values(
            scope="workspace", state="POSTGRES_AUTHORITY", generation=1,
        ))
    with pytest.raises(ActivationUnknown, match="repair forward"):
        await reconcile_activation(engine, receipt)
    async with engine.begin() as connection:
        await connection.execute(delete(workset_authority))
        await connection.execute(update(worksets).values(name="Drifted"))
    with pytest.raises(ActivationUnknown, match="repair forward"):
        await reconcile_activation(engine, receipt)
    with pytest.raises(ValueError, match="reviewed worksheet"):
        await activate(engine, "0" * 64)
    await engine.dispose()
