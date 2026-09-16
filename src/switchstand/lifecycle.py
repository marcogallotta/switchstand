"""Durable state and deterministic projection for required-result persistence."""

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, Table, Text, select, update
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from .contracts import ClosedModel
from .state import metadata, work_handles

PROFILE_TYPE = "REQUIRED_RESULT_PERSISTENCE"
PROFILE_VERSION = 1


class ProfileState(StrEnum):
    PENDING_RESULT = "PENDING_RESULT"
    PERSIST_REQUIRED = "PERSIST_REQUIRED"
    UNKNOWN = "UNKNOWN"
    TERMINAL = "TERMINAL"


class ContinuationKind(StrEnum):
    CONTINUE_CURRENT_WORK = "CONTINUE_CURRENT_WORK"
    PERSIST_RESULT = "PERSIST_RESULT"
    RECONCILE_UNKNOWN = "RECONCILE_UNKNOWN"
    TERMINAL = "TERMINAL"


class LifecycleEvent(StrEnum):
    RESULT_READY = "RESULT_READY"
    PERSIST_READBACK_MATCHED = "PERSIST_READBACK_MATCHED"
    PERSIST_OUTCOME_AMBIGUOUS = "PERSIST_OUTCOME_AMBIGUOUS"
    RECONCILIATION_NO_MATCH_SAFE_TO_RETRY = "RECONCILIATION_NO_MATCH_SAFE_TO_RETRY"
    CURRENTNESS_STALE = "CURRENTNESS_STALE"
    CURRENTNESS_UNKNOWN = "CURRENTNESS_UNKNOWN"


lifecycle_obligations = Table(
    "lifecycle_obligations",
    metadata,
    Column("obligation_id", PGUUID(as_uuid=True), primary_key=True),
    Column("profile_type", Text, nullable=False),
    Column("profile_version", Integer, nullable=False),
    Column(
        "work_id_ref",
        PGUUID(as_uuid=True),
        ForeignKey(work_handles.c.id, ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    Column("currentness_token", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("row_version", Integer, nullable=False),
    Column("destination_ref", Text, nullable=True),
    Column("result_correlation", Text, nullable=True),
    Column("authoritative_readback_evidence", Text, nullable=True),
    Column("unknown_reason", Text, nullable=True),
    Column("unknown_evidence", Text, nullable=True),
    CheckConstraint(f"profile_type = '{PROFILE_TYPE}'", name="ck_lifecycle_profile_type"),
    CheckConstraint(f"profile_version = {PROFILE_VERSION}", name="ck_lifecycle_profile_version"),
    CheckConstraint("char_length(currentness_token) > 0", name="ck_lifecycle_currentness"),
    CheckConstraint("row_version >= 1", name="ck_lifecycle_row_version"),
    CheckConstraint(
        "(destination_ref IS NULL) = (result_correlation IS NULL)",
        name="ck_lifecycle_destination_pair",
    ),
    CheckConstraint(
        "destination_ref IS NULL OR char_length(destination_ref) > 0",
        name="ck_lifecycle_destination_ref",
    ),
    CheckConstraint(
        "result_correlation IS NULL OR char_length(result_correlation) > 0",
        name="ck_lifecycle_result_correlation",
    ),
    CheckConstraint(
        "authoritative_readback_evidence IS NULL "
        "OR char_length(authoritative_readback_evidence) BETWEEN 1 AND 8000",
        name="ck_lifecycle_readback_evidence",
    ),
    CheckConstraint(
        "unknown_reason IS NULL OR unknown_reason IN "
        "('PERSIST_OUTCOME_AMBIGUOUS', 'CURRENTNESS_STALE', 'CURRENTNESS_UNKNOWN')",
        name="ck_lifecycle_unknown_reason",
    ),
    CheckConstraint(
        "unknown_evidence IS NULL OR (char_length(unknown_evidence) BETWEEN 1 AND 8000)",
        name="ck_lifecycle_unknown_evidence",
    ),
    CheckConstraint(
        "(state = 'PENDING_RESULT' "
        "AND destination_ref IS NULL "
        "AND authoritative_readback_evidence IS NULL "
        "AND unknown_reason IS NULL AND unknown_evidence IS NULL) "
        "OR (state = 'PERSIST_REQUIRED' "
        "AND destination_ref IS NOT NULL "
        "AND authoritative_readback_evidence IS NULL "
        "AND unknown_reason IS NULL AND unknown_evidence IS NULL) "
        "OR (state = 'UNKNOWN' "
        "AND authoritative_readback_evidence IS NULL "
        "AND unknown_reason IS NOT NULL AND unknown_evidence IS NOT NULL) "
        "OR (state = 'TERMINAL' "
        "AND destination_ref IS NOT NULL "
        "AND authoritative_readback_evidence IS NOT NULL "
        "AND unknown_reason IS NULL AND unknown_evidence IS NULL)",
        name="ck_lifecycle_state_fields",
    ),
)

_columns = tuple(lifecycle_obligations.c)


class LifecycleObligation(ClosedModel):
    """One immutable binding and its required-result persistence state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    obligation_id: UUID
    profile_type: Literal["REQUIRED_RESULT_PERSISTENCE"] = PROFILE_TYPE
    profile_version: Literal[1] = PROFILE_VERSION
    work_id_ref: UUID
    currentness_token: str = Field(min_length=1)
    state: ProfileState
    row_version: int = Field(ge=1)
    destination_ref: str | None = Field(default=None, min_length=1)
    result_correlation: str | None = Field(default=None, min_length=1)
    authoritative_readback_evidence: str | None = Field(default=None, min_length=1, max_length=8000)
    unknown_reason: (
        Literal["PERSIST_OUTCOME_AMBIGUOUS", "CURRENTNESS_STALE", "CURRENTNESS_UNKNOWN"] | None
    ) = None
    unknown_evidence: str | None = Field(default=None, min_length=1, max_length=8000)

    @model_validator(mode="after")
    def valid_state_fields(self) -> Self:
        paired = (self.destination_ref is None) == (self.result_correlation is None)
        if not paired:
            raise ValueError("destination_ref and result_correlation must be paired")

        has_pair = self.destination_ref is not None
        has_readback = self.authoritative_readback_evidence is not None
        has_unknown = self.unknown_reason is not None and self.unknown_evidence is not None
        partial_unknown = (self.unknown_reason is None) != (self.unknown_evidence is None)
        if partial_unknown:
            raise ValueError("UNKNOWN reason and evidence must be paired")

        valid = {
            ProfileState.PENDING_RESULT: not has_pair and not has_readback and not has_unknown,
            ProfileState.PERSIST_REQUIRED: has_pair and not has_readback and not has_unknown,
            ProfileState.UNKNOWN: not has_readback and has_unknown,
            ProfileState.TERMINAL: has_pair and has_readback and not has_unknown,
        }
        if not valid[self.state]:
            raise ValueError(f"fields do not match lifecycle state {self.state}")
        return self


class Continuation(ClosedModel):
    """The only action the closed profile projects for its current durable state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ContinuationKind
    destination_ref: str | None = Field(default=None, min_length=1)
    result_correlation: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def exact_persist_fields(self) -> Self:
        has_result = self.destination_ref is not None and self.result_correlation is not None
        partial_result = (self.destination_ref is None) != (self.result_correlation is None)
        if partial_result or (self.kind is ContinuationKind.PERSIST_RESULT) != has_result:
            raise ValueError("only PERSIST_RESULT has an exact destination and correlation")
        return self


def _obligation(row: RowMapping | None) -> LifecycleObligation | None:
    return None if row is None else LifecycleObligation.model_validate(row)


class LifecycleRepository:
    """Persist obligations with optimistic compare-and-replace semantics."""

    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def create(
        self,
        obligation_id: UUID,
        work_id_ref: UUID,
        currentness_token: str,
    ) -> LifecycleObligation:
        obligation = LifecycleObligation(
            obligation_id=obligation_id,
            work_id_ref=work_id_ref,
            currentness_token=currentness_token,
            state=ProfileState.PENDING_RESULT,
            row_version=1,
        )
        statement = (
            insert(lifecycle_obligations)
            .values(obligation.model_dump(mode="python"))
            .returning(*_columns)
        )
        async with self.engine.begin() as connection:
            row = (await connection.execute(statement)).mappings().one()
        return LifecycleObligation.model_validate(row)

    async def get(self, obligation_id: UUID) -> LifecycleObligation | None:
        statement = select(*_columns).where(lifecycle_obligations.c.obligation_id == obligation_id)
        async with self.engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().one_or_none()
        return _obligation(row)

    async def replace(
        self,
        replacement: LifecycleObligation,
        expected_row_version: int,
    ) -> LifecycleObligation:
        """Atomically replace mutable state while preserving the original binding."""
        if expected_row_version < 1:
            raise ValueError("expected_row_version must be positive")
        if replacement.row_version != expected_row_version + 1:
            raise ValueError("replacement row_version must advance exactly once")

        mutable_values = replacement.model_dump(
            mode="python",
            include={
                "state",
                "row_version",
                "destination_ref",
                "result_correlation",
                "authoritative_readback_evidence",
                "unknown_reason",
                "unknown_evidence",
            },
        )
        statement = (
            update(lifecycle_obligations)
            .where(
                lifecycle_obligations.c.obligation_id == replacement.obligation_id,
                lifecycle_obligations.c.row_version == expected_row_version,
                lifecycle_obligations.c.profile_type == replacement.profile_type,
                lifecycle_obligations.c.profile_version == replacement.profile_version,
                lifecycle_obligations.c.work_id_ref == replacement.work_id_ref,
                lifecycle_obligations.c.currentness_token == replacement.currentness_token,
            )
            .values(mutable_values)
            .returning(*_columns)
        )
        async with self.engine.begin() as connection:
            row = (await connection.execute(statement)).mappings().one_or_none()
        if row is None:
            raise ValueError("stale lifecycle obligation or changed immutable binding")
        return LifecycleObligation.model_validate(row)


def _encoded_evidence(evidence: Mapping[str, object] | None) -> str:
    if evidence is None:
        raise ValueError("event requires bounded evidence")
    encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if not encoded or len(encoded) > 8000:
        raise ValueError("event evidence must be between 1 and 8000 characters")
    return encoded


class RequiredResultPersistence:
    """Apply the closed transition table and project restart-stable continuations."""

    def __init__(self, repository: LifecycleRepository):
        self.repository = repository

    async def create(
        self,
        work_id_ref: UUID,
        currentness_token: str,
    ) -> LifecycleObligation:
        return await self.repository.create(uuid4(), work_id_ref, currentness_token)

    async def _required(self, obligation_id: UUID) -> LifecycleObligation:
        obligation = await self.repository.get(obligation_id)
        if obligation is None:
            raise ValueError("lifecycle obligation missing")
        return obligation

    async def continuation(
        self,
        obligation_id: UUID,
        currentness_token: str,
    ) -> Continuation:
        obligation = await self._required(obligation_id)
        if currentness_token != obligation.currentness_token:
            return Continuation(kind=ContinuationKind.RECONCILE_UNKNOWN)
        if obligation.state is ProfileState.PENDING_RESULT:
            return Continuation(kind=ContinuationKind.CONTINUE_CURRENT_WORK)
        if obligation.state is ProfileState.PERSIST_REQUIRED:
            return Continuation(
                kind=ContinuationKind.PERSIST_RESULT,
                destination_ref=obligation.destination_ref,
                result_correlation=obligation.result_correlation,
            )
        if obligation.state is ProfileState.UNKNOWN:
            return Continuation(kind=ContinuationKind.RECONCILE_UNKNOWN)
        return Continuation(kind=ContinuationKind.TERMINAL)

    async def transition(
        self,
        obligation_id: UUID,
        currentness_token: str,
        event: LifecycleEvent,
        *,
        destination_ref: str | None = None,
        result_correlation: str | None = None,
        evidence: Mapping[str, object] | None = None,
    ) -> LifecycleObligation:
        current = await self._required(obligation_id)
        if currentness_token != current.currentness_token:
            raise ValueError("currentness token is stale or unknown")
        if current.state is ProfileState.TERMINAL:
            raise ValueError("terminal lifecycle obligation cannot transition")

        changes: dict[str, object | None]
        if event is LifecycleEvent.RESULT_READY:
            if current.state is not ProfileState.PENDING_RESULT:
                raise ValueError("RESULT_READY requires PENDING_RESULT")
            if destination_ref is None or result_correlation is None:
                raise ValueError("RESULT_READY requires destination and result correlation")
            changes = {
                "state": ProfileState.PERSIST_REQUIRED,
                "destination_ref": destination_ref,
                "result_correlation": result_correlation,
            }
        elif event is LifecycleEvent.PERSIST_OUTCOME_AMBIGUOUS:
            if current.state is not ProfileState.PERSIST_REQUIRED:
                raise ValueError("ambiguous persistence requires PERSIST_REQUIRED")
            changes = {
                "state": ProfileState.UNKNOWN,
                "unknown_reason": event.value,
                "unknown_evidence": _encoded_evidence(evidence),
            }
        elif event in {LifecycleEvent.CURRENTNESS_STALE, LifecycleEvent.CURRENTNESS_UNKNOWN}:
            changes = {
                "state": ProfileState.UNKNOWN,
                "unknown_reason": event.value,
                "unknown_evidence": _encoded_evidence(evidence),
            }
        elif event is LifecycleEvent.RECONCILIATION_NO_MATCH_SAFE_TO_RETRY:
            if (
                current.state is not ProfileState.UNKNOWN
                or current.destination_ref is None
                or current.unknown_reason != LifecycleEvent.PERSIST_OUTCOME_AMBIGUOUS.value
            ):
                raise ValueError("safe retry requires reconciled persistence ambiguity")
            changes = {
                "state": ProfileState.PERSIST_REQUIRED,
                "unknown_reason": None,
                "unknown_evidence": None,
            }
        elif event is LifecycleEvent.PERSIST_READBACK_MATCHED:
            if current.state is ProfileState.UNKNOWN and current.unknown_reason != (
                LifecycleEvent.PERSIST_OUTCOME_AMBIGUOUS.value
            ):
                raise ValueError("persistence readback cannot clear a currentness failure")
            if current.state not in {ProfileState.PERSIST_REQUIRED, ProfileState.UNKNOWN}:
                raise ValueError("persistence readback requires a result obligation")
            if (
                evidence is None
                or evidence.get("destination_ref") != current.destination_ref
                or evidence.get("result_correlation") != current.result_correlation
            ):
                raise ValueError("authoritative readback destination or correlation does not match")
            changes = {
                "state": ProfileState.TERMINAL,
                "authoritative_readback_evidence": _encoded_evidence(evidence),
                "unknown_reason": None,
                "unknown_evidence": None,
            }
        else:  # pragma: no cover - StrEnum validation makes this defensive only.
            raise ValueError("unsupported lifecycle event")

        replacement = current.model_copy(update=changes | {"row_version": current.row_version + 1})
        return await self.repository.replace(replacement, current.row_version)
