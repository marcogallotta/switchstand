"""Live Stateful prerequisite evidence for technical product currentness."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .contracts import ClosedModel
from .grants import PrincipalContext
from .product_currentness import SourceEvidence, SourceName, StatefulAcceptanceBinding
from .secure_file import create_new_private_bytes, read_private_bytes

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
    principal_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome_actions_enabled: bool
    tool_names: tuple[str, ...]
    tools_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StatefulPersistenceSnapshot(ClosedModel):
    migration_revision: str | None
    migration_receipt_digest: str | None
    outcome_state_table: str | None


class StatefulQualificationPayload(ClosedModel):
    """Exact successful outcomes produced by the real Stateful qualifier."""

    schema_version: Literal[2] = Field(alias="schema")
    issuer: Literal["switchstand-stateful-qualifier"]
    qualification: Literal["real:authenticated-stateful-currentness-v1"]
    result: Literal["PASS"]
    runtime_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    selected_runtime_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    run_id: str = Field(min_length=1)
    principal_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    tools_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    persistence_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    basis_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    authenticated_mcp: Literal["PASS"]
    admission: Literal["DENIED"]
    stale_cas: Literal["STALE"]
    replay: Literal["REPLAYED"]
    currentness: Literal["RECHECKED"]
    observed_at: datetime


class StatefulQualificationReceipt(StatefulQualificationPayload):
    """Sealed output from the real Stateful qualification runner."""

    seal: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class StatefulQualificationEmission:
    """The exact receipt published by one successful emission."""

    receipt: StatefulQualificationReceipt
    digest: str


SnapshotReader = Callable[[], Awaitable[StatefulServerSnapshot]]
PrincipalReader = Callable[[], Awaitable[PrincipalContext | None]]


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _qualification_payload_bytes(value: StatefulQualificationPayload) -> bytes:
    return _canonical(value.model_dump(mode="json", by_alias=True, exclude={"seal"}))


def _emit_stateful_qualification_receipt(  # pyright: ignore[reportUnusedFunction]
    payload: StatefulQualificationPayload,
    *,
    key_path: Path,
    receipt_path: Path,
) -> StatefulQualificationEmission:
    """Seal and durably publish one immutable qualification receipt."""
    if not key_path.is_absolute() or not receipt_path.is_absolute():
        raise ValueError("qualification key and receipt paths must be absolute")
    if key_path.resolve(strict=False) == receipt_path.resolve(strict=False):
        raise ValueError("qualification key and receipt paths must be distinct")
    key = read_private_bytes(key_path)
    if len(key) != 32:
        raise ValueError("qualification key must contain exactly 32 bytes")
    seal = hmac.new(key, _qualification_payload_bytes(payload), hashlib.sha256).hexdigest()
    receipt = StatefulQualificationReceipt(
        **payload.model_dump(mode="python", by_alias=True), seal=seal
    )
    raw = _canonical(receipt.model_dump(mode="json", by_alias=True))
    create_new_private_bytes(receipt_path, raw)
    return StatefulQualificationEmission(receipt=receipt, digest=hashlib.sha256(raw).hexdigest())


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
        principal: PrincipalReader,
        snapshot: SnapshotReader,
        *,
        expected_migration_revision: str,
        expected_tools_schema_sha256: str,
        contract_revision: str = STATEFUL_CONTRACT_REVISION,
        migration_receipt_name: str = "work-identity-migration-complete-v1",
        qualification_receipt: Path | None = None,
        qualification_key: Path | None = None,
    ) -> None:
        if not expected_migration_revision:
            raise ValueError("expected_migration_revision must be nonempty")
        if len(expected_tools_schema_sha256) != 64:
            raise ValueError("expected_tools_schema_sha256 must be a SHA-256 digest")
        if (qualification_receipt is None) != (qualification_key is None):
            raise ValueError("qualification receipt and key must be configured together")
        self._engine = engine
        self._principal = principal
        self._snapshot = snapshot
        self._expected_migration_revision = expected_migration_revision
        self._expected_tools_schema_sha256 = expected_tools_schema_sha256
        self._contract_revision = contract_revision
        self._migration_receipt_name = migration_receipt_name
        self._qualification_receipt = qualification_receipt
        self._qualification_key = qualification_key

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
        return _digest(
            (value.runtime_sha, value.selected_runtime_sha, value.run_id, value.principal_key)
        )

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
        principal = await self._principal()
        snapshot = await self._snapshot()
        if principal is None or snapshot.principal_key != principal.key:
            raise ValueError("snapshot is not bound to the authenticated principal")
        persistence = await self._persistence()
        basis = _digest(
            {
                "snapshot": snapshot.model_dump(mode="json"),
                "persistence": persistence.model_dump(mode="json"),
                "contract_token": self._contract_token,
            }
        )
        return snapshot, persistence, basis

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

    def _qualification(self) -> tuple[StatefulQualificationReceipt, str]:
        if self._qualification_receipt is None or self._qualification_key is None:
            raise ValueError("functional proof is not configured")
        raw = read_private_bytes(self._qualification_receipt)
        if len(raw) > 64 * 1024:
            raise ValueError("qualification receipt is too large")
        receipt = StatefulQualificationReceipt.model_validate_json(raw)
        key = read_private_bytes(self._qualification_key)
        if len(key) != 32:
            raise ValueError("qualification key must contain exactly 32 bytes")
        expected = hmac.new(key, _qualification_payload_bytes(receipt), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(receipt.seal, expected):
            raise ValueError("qualification receipt seal is invalid")
        return receipt, hashlib.sha256(raw).hexdigest()

    def _functional_proof(
        self,
        snapshot: StatefulServerSnapshot,
        persistence: StatefulPersistenceSnapshot,
        basis: str,
        observed_at: datetime,
    ) -> SourceEvidence:
        try:
            receipt, token = self._qualification()
        except OSError, ValueError, ValidationError:
            return SourceEvidence(
                source="functional_proof",
                result="UNKNOWN",
                evidence_id="unavailable:functional_proof",
                observed_at=observed_at,
                detail="functional_proof_missing_or_invalid",
            )
        bound = (
            receipt.runtime_sha == snapshot.runtime_sha
            and receipt.selected_runtime_sha == snapshot.selected_runtime_sha
            and receipt.run_id == snapshot.run_id
            and receipt.principal_key == snapshot.principal_key
            and receipt.tools_schema_sha256 == snapshot.tools_schema_sha256
            and receipt.persistence_token == self.persistence_token(persistence)
            and receipt.basis_token == basis
            and receipt.contract_token == self._contract_token
        )
        return SourceEvidence(
            source="functional_proof",
            result="TRUE" if bound else "CONFLICT",
            evidence_id=f"stateful-qualification:{token}",
            currentness_token=token,
            basis_token=basis,
            observed_at=observed_at,
            detail="qualification_current" if bound else "qualification_binding_mismatch",
        )

    async def read(self, source: SourceName) -> SourceEvidence:
        snapshot, persistence, basis = await self._basis_state()
        observed_at = datetime.now().astimezone()
        if source == "functional_proof":
            return self._functional_proof(snapshot, persistence, basis, observed_at)
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
        snapshot, persistence, basis = await self._basis_state()
        if source == "functional_proof":
            proof = self._functional_proof(
                snapshot, persistence, basis, datetime.now().astimezone()
            )
            return proof.currentness_token
        if source == "persistence_ready":
            return self.persistence_token(persistence)
        return self._snapshot_token(source, snapshot)
