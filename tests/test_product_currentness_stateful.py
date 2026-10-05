from __future__ import annotations

import os
from typing import cast

import pytest
from chatgpt_fixture import PRINCIPAL
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.product_currentness import (
    STATEFUL_PRODUCT_WORK_ID,
    evaluate_stateful_currentness,
)
from switchstand.product_currentness_stateful import (
    LiveStatefulEvidenceReader,
    StatefulPersistenceSnapshot,
    StatefulServerSnapshot,
)

EXPECTED_MIGRATION = "0020_human_reviews"
SCHEMA_DIGEST = "b" * 64
SERVER_SNAPSHOT = StatefulServerSnapshot(
    runtime_sha="a" * 40,
    selected_runtime_sha="a" * 40,
    run_id="run-1",
    principal_key=PRINCIPAL.key,
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

    async def read_principal():
        return PRINCIPAL

    async def read_snapshot() -> StatefulServerSnapshot:
        return snapshot

    reader = MemoryStatefulEvidenceReader(
        cast(AsyncEngine, object()),
        read_principal,
        read_snapshot,
        expected_migration_revision=EXPECTED_MIGRATION,
        expected_tools_schema_sha256=SCHEMA_DIGEST,
    )

    def set_snapshot(value: StatefulServerSnapshot) -> None:
        nonlocal snapshot
        snapshot = value

    return reader, snapshot, set_snapshot


async def test_live_prerequisites_are_diagnostic_but_cannot_claim_true() -> None:
    reader, _, _ = build_reader()

    result = await evaluate_stateful_currentness(STATEFUL_PRODUCT_WORK_ID, reader)

    assert (result.status, result.current) == ("unknown", "UNKNOWN")
    assert result.blockers == ("functional_proof",)
    assert {condition.detail for condition in result.conditions} == {
        "runtime_selected",
        "feature_enabled",
        "schema_current",
        "persistence_current",
        "functional_proof_missing",
    }


async def test_expected_schema_and_persistence_are_contract_inputs() -> None:
    reader, snapshot, set_snapshot = build_reader()
    set_snapshot(snapshot.model_copy(update={"tools_schema_sha256": "e" * 64}))
    schema = await reader.read("schema_surface")
    assert schema.result == "FALSE" and schema.detail == "schema_mismatch"

    reader.persistence = reader.persistence.model_copy(
        update={"migration_revision": "0015_work_admission_time"}
    )
    persistence = await reader.read("persistence_ready")
    assert persistence.result == "FALSE"
    assert persistence.detail == "persistence_prerequisite_missing"


async def test_snapshot_must_match_authenticated_principal() -> None:
    reader, snapshot, set_snapshot = build_reader()
    set_snapshot(snapshot.model_copy(update={"principal_key": "d" * 64}))
    result = await evaluate_stateful_currentness(STATEFUL_PRODUCT_WORK_ID, reader)
    assert (result.status, result.current, result.reason) == (
        "unknown", "UNKNOWN", "acceptance_contract_unbound",
    )


async def test_live_adapter_reads_real_postgres_prerequisites() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL adapter test")
    engine = create_async_engine(database_url)

    async def read_snapshot() -> StatefulServerSnapshot:
        return SERVER_SNAPSHOT

    async def read_principal():
        return PRINCIPAL

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
            read_principal,
            read_snapshot,
            expected_migration_revision=actual_migration,
            expected_tools_schema_sha256=SCHEMA_DIGEST,
        )
        persistence = await reader.read("persistence_ready")
        assert persistence.result == "TRUE"
        assert persistence.currentness_token is not None
    finally:
        await engine.dispose()
