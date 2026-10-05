"""Deterministic, fail-closed technical product currentness reconciliation.

This evaluator does not decide Stateful's acceptance contract. Its evidence adapter
must supply an attributable binding; absent that binding, the answer is ``UNKNOWN``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID, uuid5

from pydantic import Field

from .contracts import ClosedModel

ConditionResult = Literal["TRUE", "FALSE", "UNKNOWN", "CONFLICT"]
ReconciliationStatus = Literal["ok", "unknown", "conflict", "denied"]
SourceName = Literal[
    "runtime_identity",
    "feature_enabled",
    "schema_surface",
    "persistence_ready",
    "functional_proof",
]

RECONCILIATION_NAMESPACE = UUID("b390ecaf-7835-5f31-8dbc-b188cb529702")
STATEFUL_PRODUCT_WORK_ID = UUID("2becc5d4-0656-4d31-8186-23bfe3861264")
UNBOUND_CONTRACT_REVISION = "UNBOUND"


class StatefulAcceptanceBinding(ClosedModel):
    """Attributable product-owned acceptance binding, or absent when unresolved."""

    contract_revision: str = Field(min_length=1)
    required_sources: tuple[SourceName, ...] = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    currentness_token: str = Field(min_length=1)
    basis_token: str = Field(min_length=1)


class SourceEvidence(ClosedModel):
    """One attributable observation and the token used to recheck it."""

    source: SourceName
    result: ConditionResult
    evidence_id: str = Field(min_length=1)
    currentness_token: str | None = Field(default=None, min_length=1)
    basis_token: str | None = Field(default=None, min_length=1)
    observed_at: datetime
    detail: str | None = Field(default=None, min_length=1)


class CurrentnessEvidenceReader(Protocol):
    async def acceptance_binding(self) -> StatefulAcceptanceBinding | None: ...

    async def basis_token(self) -> str | None: ...

    async def read(self, source: SourceName) -> SourceEvidence: ...

    async def currentness_token(self, source: SourceName) -> str | None: ...


class StatefulCondition(ClosedModel):
    name: SourceName
    result: ConditionResult
    evidence_id: str
    currentness_token: str | None = None
    observed_at: datetime
    detail: str | None = None


class ProductCurrentness(ClosedModel):
    status: ReconciliationStatus
    product_work_id: UUID
    current: ConditionResult
    contract_revision: str
    reconciliation_id: UUID
    basis_id: str
    conditions: tuple[StatefulCondition, ...]
    blockers: tuple[SourceName, ...]
    reason: str | None = None


def _unavailable(source: SourceName, reason: str) -> SourceEvidence:
    return SourceEvidence(
        source=source,
        result="UNKNOWN",
        evidence_id=f"unavailable:{source}",
        observed_at=datetime.now().astimezone(),
        detail=reason,
    )


async def _safe_read(reader: CurrentnessEvidenceReader, source: SourceName) -> SourceEvidence:
    try:
        evidence = await reader.read(source)
    except Exception as error:  # noqa: BLE001 - any source failure means UNKNOWN
        return _unavailable(source, type(error).__name__)
    if evidence.source != source:
        return _unavailable(source, "source_identity_mismatch")
    if evidence.result in {"TRUE", "FALSE"} and evidence.currentness_token is None:
        return evidence.model_copy(
            update={"result": "UNKNOWN", "detail": "currentness_token_missing"}
        )
    return evidence


async def _safe_token(reader: CurrentnessEvidenceReader, source: SourceName) -> str | None:
    try:
        return await reader.currentness_token(source)
    except Exception:  # noqa: BLE001 - any failed recheck means UNKNOWN
        return None


def _basis(
    product_work_id: UUID,
    starting_binding: StatefulAcceptanceBinding | None,
    ending_binding: StatefulAcceptanceBinding | None,
    starting_basis: str | None,
    ending_basis: str | None,
    evidence: tuple[SourceEvidence, ...],
) -> str:
    payload = {
        "product_work_id": str(product_work_id),
        "starting_contract": (
            None if starting_binding is None else starting_binding.model_dump(mode="json")
        ),
        "ending_contract": (
            None if ending_binding is None else ending_binding.model_dump(mode="json")
        ),
        "starting_basis": starting_basis,
        "ending_basis": ending_basis,
        "evidence": [
            {
                "source": item.source,
                "result": item.result,
                "evidence_id": item.evidence_id,
                "currentness_token": item.currentness_token,
                "basis_token": item.basis_token,
                "detail": item.detail,
            }
            for item in evidence
        ],
    }
    value = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def _aggregate(
    evidence: tuple[SourceEvidence, ...],
) -> tuple[ReconciliationStatus, ConditionResult, str | None]:
    results = {item.result for item in evidence}
    if "CONFLICT" in results:
        return "conflict", "CONFLICT", "conflicting_evidence"
    if "FALSE" in results:
        return "ok", "FALSE", None
    if "UNKNOWN" in results:
        return "unknown", "UNKNOWN", "evidence_or_coherence_unproved"
    return "ok", "TRUE", None


async def _safe_binding(
    reader: CurrentnessEvidenceReader,
) -> StatefulAcceptanceBinding | None:
    try:
        return await reader.acceptance_binding()
    except Exception:  # noqa: BLE001 - an unreadable binding is unbound
        return None


async def _safe_basis(reader: CurrentnessEvidenceReader) -> str | None:
    try:
        return await reader.basis_token()
    except Exception:  # noqa: BLE001 - an unreadable basis cannot prove coherence
        return None


def _terminal_without_conditions(
    product_work_id: UUID,
    *,
    status: ReconciliationStatus,
    reason: str,
    binding: StatefulAcceptanceBinding | None = None,
) -> ProductCurrentness:
    basis_id = _basis(product_work_id, binding, binding, None, None, ())
    return ProductCurrentness(
        status=status,
        product_work_id=product_work_id,
        current="UNKNOWN",
        contract_revision=(
            UNBOUND_CONTRACT_REVISION if binding is None else binding.contract_revision
        ),
        reconciliation_id=uuid5(RECONCILIATION_NAMESPACE, basis_id),
        basis_id=basis_id,
        conditions=(),
        blockers=(),
        reason=reason,
    )


async def evaluate_stateful_currentness(
    product_work_id: UUID,
    reader: CurrentnessEvidenceReader,
) -> ProductCurrentness:
    """Evaluate an attributable Stateful contract on one shared live basis."""
    if product_work_id != STATEFUL_PRODUCT_WORK_ID:
        return _terminal_without_conditions(
            product_work_id, status="denied", reason="unsupported_product"
        )

    starting_binding = await _safe_binding(reader)
    if starting_binding is None:
        return _terminal_without_conditions(
            product_work_id, status="unknown", reason="acceptance_contract_unbound"
        )

    starting_basis = await _safe_basis(reader)
    gathered = tuple(
        [await _safe_read(reader, source) for source in starting_binding.required_sources]
    )
    rechecked = tuple(
        [await _safe_token(reader, source) for source in starting_binding.required_sources]
    )
    ending_basis = await _safe_basis(reader)
    ending_binding = await _safe_binding(reader)
    shared_basis = (
        starting_basis
        if (
            starting_basis is not None
            and starting_basis == ending_basis
            and starting_binding == ending_binding
            and starting_binding.basis_token == starting_basis
        )
        else None
    )
    coherent: list[SourceEvidence] = []
    for item, token in zip(gathered, rechecked, strict=True):
        if item.result == "UNKNOWN" and item.basis_token is None:
            coherent.append(item)
        elif shared_basis is None or item.basis_token != shared_basis:
            coherent.append(
                item.model_copy(update={"result": "UNKNOWN", "detail": "evaluation_basis_unproved"})
            )
        elif item.currentness_token is None or token is None:
            coherent.append(
                item.model_copy(
                    update={"result": "UNKNOWN", "detail": "source_currentness_unavailable"}
                )
            )
        elif token != item.currentness_token:
            coherent.append(
                item.model_copy(
                    update={
                        "result": "UNKNOWN",
                        "detail": "source_changed_during_reconciliation",
                    }
                )
            )
        else:
            coherent.append(item)
    final = tuple(coherent)
    basis_id = _basis(
        product_work_id,
        starting_binding,
        ending_binding,
        starting_basis,
        ending_basis,
        final,
    )
    status, current, reason = _aggregate(final)
    blockers: tuple[SourceName, ...] = tuple(item.source for item in final if item.result != "TRUE")
    return ProductCurrentness(
        status=status,
        product_work_id=product_work_id,
        current=current,
        contract_revision=starting_binding.contract_revision,
        reconciliation_id=uuid5(RECONCILIATION_NAMESPACE, basis_id),
        basis_id=basis_id,
        conditions=tuple(
            StatefulCondition(
                name=item.source,
                result=item.result,
                evidence_id=item.evidence_id,
                currentness_token=item.currentness_token,
                observed_at=item.observed_at,
                detail=item.detail,
            )
            for item in final
        ),
        blockers=blockers,
        reason=reason,
    )
