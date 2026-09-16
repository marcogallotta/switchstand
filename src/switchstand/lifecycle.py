"""Durable storage for the fixed required-result-persistence profile."""

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

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
