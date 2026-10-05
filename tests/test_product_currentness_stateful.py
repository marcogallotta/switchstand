from __future__ import annotations

import os
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.product_currentness import (
    STATEFUL_PRODUCT_WORK_ID,
    evaluate_stateful_currentness,
)
from switchstand.product_currentness_stateful import (
    LiveStatefulEvidenceReader,
    StatefulFunctionalProof,
    StatefulPersistenceSnapshot,
    StatefulServerSnapshot,
)

EXPECTED_MIGRATION = "0020_human_reviews"
SCHEMA_DIGEST = "b" * 64
SERVER_SNAPSHOT = StatefulServerSnapshot(
    runtime_sha="a" * 40,
    selected_runtime_sha="a" * 40,
    run_id="run-1",
    outcome_actions_enabled=True,
    tool_names=("work_get", "work_update", "outcome_state_update"),
    tools_schema_sha256=SCHEMA_DIGEST,
)


class MemoryStatefulEvidenceReader(LiveStatefulEvidenceReader):
    persistence = StatefulPersistenceSnapshot(
        migration_revision=EXPECTED_MIGRATION,
        migration_receipt_digest="c" * 64,
        outcome_state_table="outcome_state_revisions",
    )

    async def _persistence(self) -> StatefulPersistenceSnapshot:
        return self.persistence


def build_reader():
    snapshot = SERVER_SNAPSHOT
    proof: StatefulFunctionalProof | None = None

    async def read_snapshot() -> StatefulServerSnapshot:
        return snapshot

    async def read_proof() -> StatefulFunctionalProof | None:
        return proof

    reader = MemoryStatefulEvidenceReader(
        cast(AsyncEngine, object()),
        read_snapshot,
        read_proof,
        expected_migration_revision=EXPECTED_MIGRATION,
        expected_tools_schema_sha256=SCHEMA_DIGEST,
    )

    def set_snapshot(value: StatefulServerSnapshot) -> None:
        nonlocal snapshot
        snapshot = value

    def set_proof(value: StatefulFunctionalProof | None) -> None:
        nonlocal proof
        proof = value

    return reader, snapshot, set_snapshot, set_proof


async def passing_proof(
    reader: MemoryStatefulEvidenceReader,
    **changes: object,
) -> StatefulFunctionalProof:
    values: dict[str, object] = {
        "evidence_id": "stateful-real-mcp-qualification",
        "result": "PASS",
        "binding": await reader.qualification_binding(),
        "authenticated_mcp_round_trip": True,
        "admission_enforced": True,
        "stale_cas_rejected": True,
        "replay_idempotent": True,
        "currentness_rechecked": True,
    }
    values.update(changes)
    return StatefulFunctionalProof.model_validate(values)


async def test_live_binding_proves_exact_technical_true_boundary() -> None:
    reader, _, _, set_proof = build_reader()
    set_proof(await passing_proof(reader))

    result = await evaluate_stateful_currentness(STATEFUL_PRODUCT_WORK_ID, reader)

    assert (result.status, result.current) == ("ok", "TRUE")
    assert result.blockers == ()
    assert {condition.detail for condition in result.conditions} == {
        "runtime_selected",
        "feature_enabled",
        "schema_current",
        "persistence_current",
        "functional_proof_pass",
    }


async def test_proof_must_cover_mcp_admission_cas_replay_and_currentness() -> None:
    for missing_check in (
        "authenticated_mcp_round_trip",
        "admission_enforced",
        "stale_cas_rejected",
        "replay_idempotent",
        "currentness_rechecked",
    ):
        reader, _, _, set_proof = build_reader()
        set_proof(await passing_proof(reader, **{missing_check: False}))
        result = await evaluate_stateful_currentness(STATEFUL_PRODUCT_WORK_ID, reader)
        proof = next(item for item in result.conditions if item.name == "functional_proof")
        assert result.current == "FALSE"
        assert proof.result == "FALSE" and proof.detail == "functional_proof_failed"


@pytest.mark.parametrize("restart", [False, True], ids=["wrong_binding", "restart"])
async def test_missing_wrong_or_restarted_proof_never_claims_true(restart: bool) -> None:
    reader, snapshot, set_snapshot, set_proof = build_reader()
    assert (await evaluate_stateful_currentness(STATEFUL_PRODUCT_WORK_ID, reader)).current == "UNKNOWN"

    valid_proof = await passing_proof(reader)
    if restart:
        set_proof(valid_proof)
        set_snapshot(snapshot.model_copy(update={"run_id": "run-2"}))
    else:
        wrong = valid_proof.binding.model_copy(update={"runtime_sha": "d" * 40})
        set_proof(valid_proof.model_copy(update={"binding": wrong}))
    result = await evaluate_stateful_currentness(STATEFUL_PRODUCT_WORK_ID, reader)
    proof = next(item for item in result.conditions if item.name == "functional_proof")
    assert (result.status, result.current) == ("conflict", "CONFLICT")
    assert proof.detail == "functional_proof_basis_mismatch"


async def test_expected_schema_and_persistence_are_contract_inputs() -> None:
    reader, snapshot, set_snapshot, _ = build_reader()
    set_snapshot(snapshot.model_copy(update={"tools_schema_sha256": "e" * 64}))
    schema = await reader.read("schema_surface")
    assert schema.result == "FALSE" and schema.detail == "schema_mismatch"

    reader.persistence = reader.persistence.model_copy(
        update={"migration_revision": "0015_work_admission_time"}
    )
    persistence = await reader.read("persistence_ready")
    assert persistence.result == "FALSE"
    assert persistence.detail == "persistence_prerequisite_missing"


async def test_live_adapter_reads_real_postgres_prerequisites() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL adapter test")
    engine = create_async_engine(database_url)

    async def read_snapshot() -> StatefulServerSnapshot:
        return SERVER_SNAPSHOT

    async def read_proof() -> StatefulFunctionalProof | None:
        return None

    try:
        async with engine.begin() as connection:
            actual_migration = str(
                (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            )
            await connection.execute(
                text("""
                    INSERT INTO work_migration_receipts (name, source_digest)
                    VALUES ('work-identity-migration-complete-v1', :digest)
                    ON CONFLICT (name)
                    DO UPDATE SET source_digest = EXCLUDED.source_digest
                """),
                {"digest": "c" * 64},
            )
        reader = LiveStatefulEvidenceReader(
            engine,
            read_snapshot,
            read_proof,
            expected_migration_revision=actual_migration,
            expected_tools_schema_sha256=SCHEMA_DIGEST,
        )
        persistence = await reader.read("persistence_ready")
        assert persistence.result == "TRUE"
        assert persistence.currentness_token is not None
    finally:
        await engine.dispose()
