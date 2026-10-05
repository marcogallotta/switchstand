"""Live Stateful prerequisite evidence for technical product currentness."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .contracts import ClosedModel
from .product_currentness import (
    ConditionResult,
    SourceEvidence,
    SourceName,
    StatefulAcceptanceBinding,
)

STATEFUL_CONTRACT_REVISION = "stateful-technical-currentness-v1"
STATEFUL_REQUIRED_SOURCES: tuple[SourceName, ...] = (
    "runtime_identity",
    "feature_enabled",
    "schema_surface",
    "persistence_ready",
    "functional_proof",
)


class StatefulServerSnapshot(ClosedModel):
    """One authenticated read of the selected Stateful MCP process."""

    runtime_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    selected_runtime_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    run_id: str = Field(min_length=1)
    outcome_actions_enabled: bool
    tool_names: tuple[str, ...]
    tools_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StatefulPersistenceSnapshot(ClosedModel):
    migration_revision: str | None
    migration_receipt_digest: str | None
    outcome_state_table: str | None


class StatefulQualificationBinding(ClosedModel):
    """Live values an external qualification must bind into its proof receipt."""

    runtime_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    run_id: str = Field(min_length=1)
    tools_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    persistence_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    basis_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    currentness_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class StatefulFunctionalProof(ClosedModel):
    """Receipt from a real MCP qualification, not a model or unit-test assertion."""

    evidence_id: str = Field(min_length=1)
    result: Literal["PASS", "FAIL", "UNKNOWN", "CONFLICT"]
    binding: StatefulQualificationBinding
    authenticated_mcp_round_trip: bool
    admission_enforced: bool
    stale_cas_rejected: bool
    replay_idempotent: bool
    currentness_rechecked: bool


SnapshotReader = Callable[[], Awaitable[StatefulServerSnapshot]]
ProofReader = Callable[[], Awaitable[StatefulFunctionalProof | None]]


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class LiveStatefulEvidenceReader:
    """Bind selected runtime and PostgreSQL prerequisites to one release basis.

    This first layer deliberately leaves functional proof UNKNOWN. It can diagnose
    whether the selected live runtime is ready for qualification, but cannot claim
    the product's technical TRUE boundary until a bound real qualification is added.
    """

    _required_tools = frozenset({"work_get", "work_update", "outcome_state_update"})
    _forbidden_tools = frozenset({"enriched_work_get", "enriched_work_update"})

    def __init__(
        self,
        engine: AsyncEngine,
        snapshot: SnapshotReader,
        functional_proof: ProofReader,
        *,
        expected_migration_revision: str,
        expected_tools_schema_sha256: str,
        contract_revision: str = STATEFUL_CONTRACT_REVISION,
        migration_receipt_name: str = "work-identity-migration-complete-v1",
    ) -> None:
        if not expected_migration_revision:
            raise ValueError("expected_migration_revision must be nonempty")
        if len(expected_tools_schema_sha256) != 64:
            raise ValueError("expected_tools_schema_sha256 must be a SHA-256 digest")
        self._engine = engine
        self._snapshot = snapshot
        self._functional_proof = functional_proof
        self._expected_migration_revision = expected_migration_revision
        self._expected_tools_schema_sha256 = expected_tools_schema_sha256
        self._contract_revision = contract_revision
        self._migration_receipt_name = migration_receipt_name

    @property
    def _contract_token(self) -> str:
        return _digest(
            {
                "contract_revision": self._contract_revision,
                "required_sources": STATEFUL_REQUIRED_SOURCES,
                "expected_migration_revision": self._expected_migration_revision,
                "expected_tools_schema_sha256": self._expected_tools_schema_sha256,
                "migration_receipt_name": self._migration_receipt_name,
                "required_tools": sorted(self._required_tools),
                "forbidden_tools": sorted(self._forbidden_tools),
            }
        )

    async def _persistence(self) -> StatefulPersistenceSnapshot:
        statement = text("""
            SELECT
              (SELECT version_num FROM alembic_version LIMIT 1),
              (SELECT source_digest FROM work_migration_receipts
                 WHERE name = :receipt_name LIMIT 1),
              to_regclass('public.outcome_state_revisions')::text
        """)
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(statement, {"receipt_name": self._migration_receipt_name})
            ).one()
        return StatefulPersistenceSnapshot(
            migration_revision=None if row[0] is None else str(row[0]),
            migration_receipt_digest=None if row[1] is None else str(row[1]),
            outcome_state_table=None if row[2] is None else str(row[2]),
        )

    @staticmethod
    def persistence_token(value: StatefulPersistenceSnapshot) -> str:
        return _digest(value.model_dump(mode="json"))

    def _persistence_ready(self, value: StatefulPersistenceSnapshot) -> bool:
        return (
            value.migration_revision == self._expected_migration_revision
            and value.migration_receipt_digest is not None
            and len(value.migration_receipt_digest) == 64
            and value.outcome_state_table == "outcome_state_revisions"
        )

    @staticmethod
    def _runtime_token(value: StatefulServerSnapshot) -> str:
        return _digest((value.runtime_sha, value.selected_runtime_sha, value.run_id))

    @classmethod
    def _snapshot_token(cls, source: SourceName, value: StatefulServerSnapshot) -> str:
        runtime = cls._runtime_token(value)
        if source == "runtime_identity":
            return runtime
        if source == "feature_enabled":
            return _digest((runtime, value.outcome_actions_enabled))
        if source == "schema_surface":
            return _digest((runtime, value.tools_schema_sha256, sorted(value.tool_names)))
        raise ValueError("source is not process-owned")

    async def _basis_state(
        self,
    ) -> tuple[StatefulServerSnapshot, StatefulPersistenceSnapshot, str]:
        snapshot = await self._snapshot()
        persistence = await self._persistence()
        basis = _digest(
            {
                "snapshot": snapshot.model_dump(mode="json"),
                "persistence": persistence.model_dump(mode="json"),
                "contract_token": self._contract_token,
            }
        )
        return snapshot, persistence, basis

    def _qualification_binding(
        self,
        snapshot: StatefulServerSnapshot,
        persistence: StatefulPersistenceSnapshot,
        basis: str,
    ) -> StatefulQualificationBinding:
        return StatefulQualificationBinding(
            runtime_sha=snapshot.runtime_sha,
            run_id=snapshot.run_id,
            tools_schema_sha256=snapshot.tools_schema_sha256,
            persistence_token=self.persistence_token(persistence),
            basis_token=basis,
            currentness_token=self._runtime_token(snapshot),
        )

    async def qualification_binding(self) -> StatefulQualificationBinding:
        """Capture the exact live runtime/persistence basis a proof must attest."""
        snapshot, persistence, basis = await self._basis_state()
        return self._qualification_binding(snapshot, persistence, basis)

    async def acceptance_binding(self) -> StatefulAcceptanceBinding:
        _, _, basis = await self._basis_state()
        return StatefulAcceptanceBinding(
            contract_revision=self._contract_revision,
            required_sources=STATEFUL_REQUIRED_SOURCES,
            evidence_id=f"stateful-contract:{self._contract_token}",
            currentness_token=self._contract_token,
            basis_token=basis,
        )

    async def basis_token(self) -> str:
        _, _, basis = await self._basis_state()
        return basis

    async def _proof_evidence(
        self,
        snapshot: StatefulServerSnapshot,
        persistence: StatefulPersistenceSnapshot,
        basis: str,
        observed_at: datetime,
    ) -> SourceEvidence:
        proof = await self._functional_proof()
        if proof is None:
            return SourceEvidence(
                source="functional_proof",
                result="UNKNOWN",
                evidence_id="unavailable:functional_proof",
                observed_at=observed_at,
                detail="functional_proof_missing",
            )
        live_binding = self._qualification_binding(snapshot, persistence, basis)
        bound = proof.binding == live_binding and proof.binding.runtime_sha == snapshot.selected_runtime_sha
        checks_pass = all(
            (
                proof.authenticated_mcp_round_trip,
                proof.admission_enforced,
                proof.stale_cas_rejected,
                proof.replay_idempotent,
                proof.currentness_rechecked,
            )
        )
        if not bound:
            result: ConditionResult = "CONFLICT"
            detail = "functional_proof_basis_mismatch"
        elif proof.result == "CONFLICT":
            result = "CONFLICT"
            detail = "functional_proof_conflict"
        elif proof.result == "UNKNOWN":
            result = "UNKNOWN"
            detail = "functional_proof_unknown"
        elif proof.result == "FAIL" or not checks_pass:
            result = "FALSE"
            detail = "functional_proof_failed"
        else:
            result = "TRUE"
            detail = "functional_proof_pass"
        return SourceEvidence(
            source="functional_proof",
            result=result,
            evidence_id=proof.evidence_id,
            currentness_token=_digest(proof.model_dump(mode="json")),
            basis_token=basis,
            observed_at=observed_at,
            detail=detail,
        )

    async def read(self, source: SourceName) -> SourceEvidence:
        snapshot, persistence, basis = await self._basis_state()
        observed_at = datetime.now().astimezone()
        if source == "functional_proof":
            return await self._proof_evidence(snapshot, persistence, basis, observed_at)
        if source == "persistence_ready":
            ready = self._persistence_ready(persistence)
            token = self.persistence_token(persistence)
            return SourceEvidence(
                source=source,
                result="TRUE" if ready else "FALSE",
                evidence_id=f"postgres:{token}",
                currentness_token=token,
                basis_token=basis,
                observed_at=observed_at,
                detail="persistence_current" if ready else "persistence_prerequisite_missing",
            )
        token = self._snapshot_token(source, snapshot)
        if source == "runtime_identity":
            valid = snapshot.runtime_sha == snapshot.selected_runtime_sha
            detail = "runtime_selected" if valid else "runtime_not_selected"
        elif source == "feature_enabled":
            valid = snapshot.outcome_actions_enabled
            detail = "feature_enabled" if valid else "feature_default_off"
        else:
            names = frozenset(snapshot.tool_names)
            valid = (
                snapshot.tools_schema_sha256 == self._expected_tools_schema_sha256
                and self._required_tools <= names
                and not self._forbidden_tools & names
            )
            detail = "schema_current" if valid else "schema_mismatch"
        return SourceEvidence(
            source=source,
            result="TRUE" if valid else "FALSE",
            evidence_id=f"server:{token}",
            currentness_token=token,
            basis_token=basis,
            observed_at=observed_at,
            detail=detail,
        )

    async def currentness_token(self, source: SourceName) -> str | None:
        snapshot, persistence, _ = await self._basis_state()
        if source == "functional_proof":
            proof = await self._functional_proof()
            return None if proof is None else _digest(proof.model_dump(mode="json"))
        if source == "persistence_ready":
            return self.persistence_token(persistence)
        return self._snapshot_token(source, snapshot)
