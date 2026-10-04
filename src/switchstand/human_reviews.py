"""Inert durable Human Review consequence state; no public submission route."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Literal, cast
from uuid import UUID, uuid5

from pydantic import Field, model_validator
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Table,
    Text,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .canonical_work import CanonicalWorkRepository, canonical_metadata, canonical_revision
from .contracts import ClosedModel

CONSEQUENCE_NAMESPACE = UUID("7dfd482c-1705-4a60-b684-0ea40db05e2e")

human_review_consequences = Table(
    "human_review_consequences",
    canonical_metadata,
    Column("consequence_id", PGUUID(as_uuid=True), primary_key=True),
    Column(
        "package_work_id",
        PGUUID(as_uuid=True),
        ForeignKey("canonical_work.work_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    Column("package_revision", Text, nullable=False),
    Column("consequence_digest", Text, nullable=False),
    Column("consequence", JSONB, nullable=False),
    Column("decision", Text),
    Column("state", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("decided_at", DateTime(timezone=True)),
    CheckConstraint("package_revision <> ''", name="ck_human_review_package_revision"),
    CheckConstraint("length(consequence_digest) = 64", name="ck_human_review_digest"),
    CheckConstraint(
        "decision IS NULL OR decision IN ('APPROVED', 'WAIT', 'HOLD', 'NO_DISPATCH')",
        name="ck_human_review_decision",
    ),
    CheckConstraint(
        "state IN ('PENDING', 'READY_FOR_IMPLEMENTATION', 'WAIT', 'HOLD', 'NO_DISPATCH')",
        name="ck_human_review_state",
    ),
    CheckConstraint(
        "(decision IS NULL AND state = 'PENDING' AND decided_at IS NULL) OR "
        "(decision IS NOT NULL AND state <> 'PENDING' AND decided_at IS NOT NULL)",
        name="ck_human_review_terminal_shape",
    ),
)


class HumanReviewConsequence(ClosedModel):
    """The exact server-owned meaning of approving one package revision."""

    package_work_id: UUID
    package_revision: str = Field(min_length=1, max_length=512)
    implementation_scope: tuple[str, ...] = Field(min_length=1, max_length=32)
    implementation_target: str = Field(min_length=1, max_length=1024)
    excluded_effects: tuple[str, ...] = Field(min_length=1, max_length=32)
    approval_effect: Literal["READY_FOR_IMPLEMENTATION_ONLY"] = "READY_FOR_IMPLEMENTATION_ONLY"

    @model_validator(mode="after")
    def bounded_unique_items(self) -> HumanReviewConsequence:
        for values in (self.implementation_scope, self.excluded_effects):
            if len(set(values)) != len(values) or any(not value or len(value) > 512 for value in values):
                raise ValueError("consequence items must be unique strings of 1..512 characters")
        return self

    @property
    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    @property
    def consequence_id(self) -> UUID:
        return uuid5(CONSEQUENCE_NAMESPACE, self.digest)


HumanDecision = Literal["APPROVED", "WAIT", "HOLD", "NO_DISPATCH"]
FailureReason = Literal[
    "package_not_found",
    "package_revision_changed",
    "consequence_unavailable",
    "consequence_changed",
    "decision_conflict",
    "state_unavailable",
]
HumanConsequenceReader = Callable[
    [AsyncConnection, UUID], Awaitable[HumanReviewConsequence | None]
]


class HumanReviewRecord(ClosedModel):
    consequence_id: UUID
    package_work_id: UUID
    package_revision: str
    consequence_digest: str
    consequence: HumanReviewConsequence
    decision: HumanDecision | None
    state: Literal["PENDING", "READY_FOR_IMPLEMENTATION", "WAIT", "HOLD", "NO_DISPATCH"]


class HumanReviewResult(ClosedModel):
    status: Literal["PREPARED", "REPLAYED", "RECORDED", "STALE", "DENIED", "CONFLICT", "UNKNOWN"]
    record: HumanReviewRecord | None = None
    reason: FailureReason | None = None

    @model_validator(mode="after")
    def exact_shape(self) -> HumanReviewResult:
        ok = self.status in {"PREPARED", "REPLAYED", "RECORDED"}
        if ok != (self.record is not None and self.reason is None):
            raise ValueError("result payload does not match status")
        if not ok and self.reason is None:
            raise ValueError("failed result requires a reason")
        return self


def _record(row: RowMapping) -> HumanReviewRecord:
    consequence = HumanReviewConsequence.model_validate(row["consequence"])
    if (
        consequence.digest != row["consequence_digest"]
        or consequence.consequence_id != row["consequence_id"]
        or consequence.package_work_id != row["package_work_id"]
        or consequence.package_revision != row["package_revision"]
    ):
        raise ValueError("stored Human Review consequence is inconsistent")
    return HumanReviewRecord(
        consequence_id=cast(UUID, row["consequence_id"]),
        package_work_id=cast(UUID, row["package_work_id"]),
        package_revision=cast(str, row["package_revision"]),
        consequence_digest=cast(str, row["consequence_digest"]),
        consequence=consequence,
        decision=cast(HumanDecision | None, row["decision"]),
        state=cast(
            Literal["PENDING", "READY_FOR_IMPLEMENTATION", "WAIT", "HOLD", "NO_DISPATCH"],
            row["state"],
        ),
    )


class HumanReviewState:
    """Atomic consequence preparation and decision storage for a future trusted shell."""

    def __init__(
        self,
        engine: AsyncEngine,
        works: CanonicalWorkRepository,
        consequence_reader: HumanConsequenceReader,
    ):
        self.engine = engine
        self.works = works
        self.consequence_reader = consequence_reader

    async def _current(
        self, connection: AsyncConnection, package_work_id: UUID, package_revision: str
    ) -> tuple[HumanReviewConsequence | None, FailureReason | None]:
        package = await self.works.get_locked(connection, package_work_id)
        if package is None:
            return None, "package_not_found"
        if canonical_revision(package.work_id, package.row_version) != package_revision:
            return None, "package_revision_changed"
        consequence = await self.consequence_reader(connection, package_work_id)
        if consequence is None:
            return None, "consequence_unavailable"
        if (
            consequence.package_work_id != package_work_id
            or consequence.package_revision != package_revision
        ):
            return None, "consequence_changed"
        return consequence, None

    async def prepare(
        self, package_work_id: UUID, package_revision: str
    ) -> HumanReviewResult:
        try:
            async with self.engine.begin() as connection:
                consequence, reason = await self._current(
                    connection, package_work_id, package_revision
                )
                if consequence is None:
                    assert reason is not None
                    if reason == "consequence_unavailable":
                        return HumanReviewResult(status="UNKNOWN", reason=reason)
                    return HumanReviewResult(
                        status="STALE" if reason != "package_not_found" else "DENIED",
                        reason=reason,
                    )
                values = {
                    "consequence_id": consequence.consequence_id,
                    "package_work_id": package_work_id,
                    "package_revision": package_revision,
                    "consequence_digest": consequence.digest,
                    "consequence": consequence.model_dump(mode="json"),
                    "state": "PENDING",
                }
                row = (await connection.execute(
                    insert(human_review_consequences).values(values).on_conflict_do_nothing().returning(
                        *human_review_consequences.c
                    )
                )).mappings().one_or_none()
                if row is not None:
                    return HumanReviewResult(status="PREPARED", record=_record(row))
                existing = (await connection.execute(select(human_review_consequences).where(
                    human_review_consequences.c.consequence_id == consequence.consequence_id
                ))).mappings().one_or_none()
                if existing is None or existing["consequence_digest"] != consequence.digest:
                    return HumanReviewResult(status="CONFLICT", reason="state_unavailable")
                return HumanReviewResult(status="REPLAYED", record=_record(existing))
        except (SQLAlchemyError, TypeError, ValueError):
            return HumanReviewResult(status="UNKNOWN", reason="state_unavailable")

    async def submit(
        self,
        consequence_id: UUID,
        package_work_id: UUID,
        package_revision: str,
        decision: HumanDecision,
    ) -> HumanReviewResult:
        try:
            async with self.engine.begin() as connection:
                consequence, reason = await self._current(
                    connection, package_work_id, package_revision
                )
                if consequence is None:
                    assert reason is not None
                    if reason == "consequence_unavailable":
                        return HumanReviewResult(status="UNKNOWN", reason=reason)
                    return HumanReviewResult(status="STALE", reason=reason)
                row = (await connection.execute(select(human_review_consequences).where(
                    human_review_consequences.c.consequence_id == consequence_id
                ).with_for_update())).mappings().one_or_none()
                if row is None:
                    return HumanReviewResult(status="DENIED", reason="consequence_unavailable")
                if (
                    row["package_work_id"] != package_work_id
                    or row["package_revision"] != package_revision
                    or consequence.consequence_id != consequence_id
                    or consequence.digest != row["consequence_digest"]
                ):
                    return HumanReviewResult(status="STALE", reason="consequence_changed")
                if row["decision"] is not None:
                    if row["decision"] != decision:
                        return HumanReviewResult(status="CONFLICT", reason="decision_conflict")
                    return HumanReviewResult(status="REPLAYED", record=_record(row))
                state = "READY_FOR_IMPLEMENTATION" if decision == "APPROVED" else decision
                decided = (await connection.execute(update(human_review_consequences).where(
                    human_review_consequences.c.consequence_id == consequence_id
                ).values(decision=decision, state=state, decided_at=func.now()).returning(
                    *human_review_consequences.c
                ))).mappings().one()
                return HumanReviewResult(status="RECORDED", record=_record(decided))
        except (SQLAlchemyError, TypeError, ValueError):
            return HumanReviewResult(status="UNKNOWN", reason="state_unavailable")
