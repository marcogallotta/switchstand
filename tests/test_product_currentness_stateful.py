from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from pathlib import Path
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
    StatefulQualificationReceipt,
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


def build_reader(*, receipt: Path | None = None, key: Path | None = None):
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
        qualification_receipt=receipt,
        qualification_key=key,
    )

    def set_snapshot(value: StatefulServerSnapshot) -> None:
        nonlocal snapshot
        snapshot = value

    return reader, snapshot, set_snapshot


async def qualified_reader(
    tmp_path: Path, **updates: object,
) -> tuple[MemoryStatefulEvidenceReader, StatefulQualificationReceipt]:
    diagnostic, _, _ = build_reader()
    binding = await diagnostic.acceptance_binding()
    receipt = StatefulQualificationReceipt(
        schema=2,
        issuer="switchstand-stateful-qualifier",
        qualification="real:authenticated-stateful-currentness-v1",
        result="PASS",
        runtime_sha=SERVER_SNAPSHOT.runtime_sha,
        selected_runtime_sha=SERVER_SNAPSHOT.selected_runtime_sha,
        run_id=SERVER_SNAPSHOT.run_id,
        principal_key=SERVER_SNAPSHOT.principal_key,
        tools_schema_sha256=SERVER_SNAPSHOT.tools_schema_sha256,
        persistence_token=diagnostic.persistence_token(diagnostic.persistence),
        basis_token=binding.basis_token,
        contract_token=binding.currentness_token,
        authenticated_mcp="PASS",
        admission="DENIED",
        stale_cas="STALE",
        replay="REPLAYED",
        currentness="RECHECKED",
        observed_at=datetime.now(UTC),
        seal="0" * 64,
    ).model_copy(update=updates)
    key = tmp_path / "qualification.key"
    key.write_bytes(b"q" * 32)
    key.chmod(0o600)
    payload = receipt.model_dump(mode="json", by_alias=True, exclude={"seal"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    receipt = receipt.model_copy(
        update={"seal": hmac.new(key.read_bytes(), canonical, hashlib.sha256).hexdigest()}
    )
    path = tmp_path / "qualification.json"
    path.write_text(receipt.model_dump_json(by_alias=True))
    path.chmod(0o600)
    reader, _, _ = build_reader(receipt=path, key=key)
    return reader, receipt


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
        "functional_proof_missing_or_invalid",
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


async def test_sealed_real_qualification_completes_true_boundary(tmp_path: Path) -> None:
    reader, _ = await qualified_reader(tmp_path)

    result = await evaluate_stateful_currentness(STATEFUL_PRODUCT_WORK_ID, reader)

    assert (result.status, result.current, result.blockers) == ("ok", "TRUE", ())
    assert result.conditions[-1].detail == "qualification_current"


@pytest.mark.parametrize("field", ["run_id", "principal_key"])
async def test_qualification_for_wrong_run_or_principal_conflicts(
    tmp_path: Path, field: str,
) -> None:
    update = "another-run" if field == "run_id" else "d" * 64
    reader, _ = await qualified_reader(tmp_path, **{field: update})

    result = await evaluate_stateful_currentness(STATEFUL_PRODUCT_WORK_ID, reader)

    assert (result.status, result.current) == ("conflict", "CONFLICT")
    assert result.conditions[-1].detail == "qualification_binding_mismatch"


async def test_unsealed_or_legacy_assertions_cannot_claim_true(tmp_path: Path) -> None:
    reader, receipt = await qualified_reader(tmp_path)
    path = tmp_path / "qualification.json"
    path.write_text(receipt.model_copy(update={"seal": "f" * 64}).model_dump_json())
    assert (await reader.read("functional_proof")).result == "UNKNOWN"

    path.write_text(json.dumps({"schema": 1, "result": "PASS", "principal_proof": "NOT_RUN"}))
    assert (await reader.read("functional_proof")).result == "UNKNOWN"


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
