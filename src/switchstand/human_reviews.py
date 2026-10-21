"""Inert durable Human Review consequence state; no public submission route."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, cast
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
        "(decision = 'APPROVED' AND state = 'READY_FOR_IMPLEMENTATION' "
        "AND decided_at IS NOT NULL) OR "
        "(decision IN ('WAIT', 'HOLD', 'NO_DISPATCH') AND state = decision "
        "AND decided_at IS NOT NULL)",
        name="ck_human_review_terminal_shape",
    ),
)


BoundedBaselineText = Annotated[str, Field(min_length=1, max_length=1024, pattern=r"\S")]


class HumanReviewOpeningV2(ClosedModel):
    what_you_asked: BoundedBaselineText
    what_this_does: BoundedBaselineText
    what_this_does_not_or_changes_for_you: BoundedBaselineText
    size: BoundedBaselineText


class HumanReviewDecisionContextV2(ClosedModel):
    size_or_burden: tuple[BoundedBaselineText, ...] = Field(min_length=1, max_length=16)
    scale_assumptions: tuple[BoundedBaselineText, ...] = Field(max_length=16)
    material_risks_and_boundaries: tuple[BoundedBaselineText, ...] = Field(max_length=32)


class HumanReviewBaselineV2(ClosedModel):
    schema_version: Literal["2"] = "2"
    human_source_refs: tuple[BoundedBaselineText, ...] = Field(min_length=1, max_length=32)
    opening: HumanReviewOpeningV2
    approved_meaning: tuple[BoundedBaselineText, ...] = Field(min_length=1, max_length=32)
    protected_requirements: tuple[BoundedBaselineText, ...] = Field(min_length=1, max_length=64)
    approved_non_goals: tuple[BoundedBaselineText, ...] = Field(max_length=32)
    decision_context: HumanReviewDecisionContextV2
    continuation_effects: tuple[Literal["IMPLEMENT", "LAND", "ACTIVATE"], ...] = Field(
        min_length=1, max_length=3
    )
    supersedes_baseline_id: UUID | None = None

    @model_validator(mode="after")
    def unique_items(self) -> HumanReviewBaselineV2:
        for values in (
            self.human_source_refs,
            self.approved_meaning,
            self.protected_requirements,
            self.approved_non_goals,
            self.continuation_effects,
            self.decision_context.size_or_burden,
            self.decision_context.scale_assumptions,
            self.decision_context.material_risks_and_boundaries,
        ):
            if len(set(values)) != len(values):
                raise ValueError("baseline tuple items must be unique")
        return self


class HumanReviewRequester(ClosedModel):
    name: str = Field(min_length=1, max_length=80)
    mailbox_id: UUID


class HumanReviewConsequence(ClosedModel):
    """The exact server-owned meaning of approving one package revision."""

    package_work_id: UUID
    package_revision: str = Field(min_length=1, max_length=512)
    implementation_scope: tuple[str, ...] = Field(min_length=1, max_length=32)
    implementation_target: str = Field(min_length=1, max_length=1024)
    excluded_effects: tuple[str, ...] = Field(min_length=1, max_length=32)
    approval_effect: Literal[
        "READY_FOR_IMPLEMENTATION_ONLY",
        "CLEAR_HUMAN_REVIEW_FOR_NAMED_CONTINUATION",
    ] = "READY_FOR_IMPLEMENTATION_ONLY"
    baseline: HumanReviewBaselineV2 | None = None
    requester: HumanReviewRequester | None = None
    task_text: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def bounded_unique_items(self) -> HumanReviewConsequence:
        if self.task_text is not None and self.baseline is None:
            raise ValueError("Task text requires an approved-baseline consequence")
        for values in (self.implementation_scope, self.excluded_effects):
            if len(set(values)) != len(values) or any(not value or len(value) > 512 for value in values):
                raise ValueError("consequence items must be unique strings of 1..512 characters")
        if self.baseline is None:
            if self.approval_effect != "READY_FOR_IMPLEMENTATION_ONLY":
                raise ValueError("V2 approval effect requires a baseline")
            return self
        if self.approval_effect != "CLEAR_HUMAN_REVIEW_FOR_NAMED_CONTINUATION":
            raise ValueError("V2 baseline requires named-continuation approval effect")
        excluded = {value.strip().casefold() for value in self.excluded_effects}
        effect_exclusions = {
            "IMPLEMENT": {"implement", "implementation"}, "LAND": {"land", "landing"},
            "ACTIVATE": {"activate", "activation"},
        }
        for effect in self.baseline.continuation_effects:
            if excluded & effect_exclusions[effect]:
                raise ValueError("continuation effect is also excluded")
        return self

    @property
    def digest(self) -> str:
        values = self.model_dump(mode="json")
        if self.task_text is None:
            values.pop("task_text")
        if self.requester is None:
            values.pop("requester")  # Preserve the exact digest of existing consequences.
        payload = json.dumps(
            values, sort_keys=True, separators=(",", ":"), allow_nan=False
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
    "baseline_supersession_required",
    "baseline_supersession_invalid",
    "agent_notes_overflow",
    "task_text_required",
    "state_unavailable",
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


def human_review_record(row: RowMapping) -> HumanReviewRecord:
    consequence = HumanReviewConsequence.model_validate(row["consequence"])
    decision = cast(HumanDecision | None, row["decision"])
    expected_state = (
        "PENDING" if decision is None
        else "READY_FOR_IMPLEMENTATION" if decision == "APPROVED" else decision
    )
    if (
        consequence.digest != row["consequence_digest"]
        or consequence.consequence_id != row["consequence_id"]
        or consequence.package_work_id != row["package_work_id"]
        or consequence.package_revision != row["package_revision"]
        or row["state"] != expected_state
    ):
        raise ValueError("stored Human Review consequence is inconsistent")
    return HumanReviewRecord(
        consequence_id=cast(UUID, row["consequence_id"]),
        package_work_id=cast(UUID, row["package_work_id"]),
        package_revision=cast(str, row["package_revision"]),
        consequence_digest=cast(str, row["consequence_digest"]),
        consequence=consequence,
        decision=decision,
        state=cast(
            Literal["PENDING", "READY_FOR_IMPLEMENTATION", "WAIT", "HOLD", "NO_DISPATCH"],
            row["state"],
        ),
    )


async def approved_baseline_in_transaction(
    connection: AsyncConnection, baseline_id: UUID, package_work_id: UUID,
) -> HumanReviewRecord | None:
    query = select(human_review_consequences).where(
        human_review_consequences.c.consequence_id == baseline_id
    )
    row = (await connection.execute(query)).mappings().one_or_none()
    if row is None:
        return None
    record = human_review_record(row)
    if (
        record.package_work_id != package_work_id
        or record.decision != "APPROVED"
        or record.consequence.baseline is None
    ):
        return None
    return record


async def current_approved_baseline_in_transaction(
    connection: AsyncConnection, package_work_id: UUID,
) -> HumanReviewRecord | None:
    rows = (await connection.execute(select(human_review_consequences).where(
        human_review_consequences.c.package_work_id == package_work_id,
        human_review_consequences.c.decision == "APPROVED",
    ).with_for_update())).mappings().all()
    approved = [human_review_record(row) for row in rows]
    v2 = [record for record in approved if record.consequence.baseline is not None]
    superseded = {
        record.consequence.baseline.supersedes_baseline_id for record in v2
        if record.consequence.baseline is not None
        and record.consequence.baseline.supersedes_baseline_id is not None
    }
    current = [record for record in v2 if record.consequence_id not in superseded]
    if len(current) > 1:
        raise ValueError("multiple current approved baselines")
    return current[0] if current else None


async def agent_notes_fit(connection: AsyncConnection, work_id: UUID, notes: str) -> bool:
    approved = await current_approved_baseline_in_transaction(connection, work_id)
    return (approved is None or approved.consequence.task_text is None
            or len(notes.encode("utf-8")) <= 2048)


class HumanReviewState:
    """Atomic consequence preparation and decision storage for a future trusted shell."""

    def __init__(
        self,
        engine: AsyncEngine,
        works: CanonicalWorkRepository,
    ):
        self.engine = engine
        self.works = works

    async def _package_reason(
        self, connection: AsyncConnection, package_work_id: UUID, package_revision: str
    ) -> FailureReason | None:
        package = await self.works.get_locked(connection, package_work_id)
        if package is None:
            return "package_not_found"
        if canonical_revision(package.work_id, package.row_version) != package_revision:
            return "package_revision_changed"
        return None

    @staticmethod
    async def _proposals(
        connection: AsyncConnection, package_work_id: UUID, package_revision: str
    ) -> list[RowMapping]:
        return list((await connection.execute(select(human_review_consequences).where(
            human_review_consequences.c.package_work_id == package_work_id,
            human_review_consequences.c.package_revision == package_revision,
        ).with_for_update())).mappings().all())

    @staticmethod
    async def _baseline_supersession_reason(
        connection: AsyncConnection, consequence: HumanReviewConsequence
    ) -> FailureReason | None:
        baseline = consequence.baseline
        if baseline is None:
            return None
        rows = (await connection.execute(
            select(human_review_consequences).where(
                human_review_consequences.c.package_work_id == consequence.package_work_id,
                human_review_consequences.c.decision == "APPROVED",
            ).with_for_update()
        )).mappings().all()
        approved = [human_review_record(row) for row in rows]
        approved_v2 = [record for record in approved if record.consequence.baseline is not None]
        referenced = {
            record.consequence.baseline.supersedes_baseline_id
            for record in approved_v2
            if record.consequence.baseline is not None
            and record.consequence.baseline.supersedes_baseline_id is not None
        }
        current = [record for record in approved_v2 if record.consequence_id not in referenced]
        if len(current) > 1:
            raise ValueError("multiple current approved baselines")
        if baseline.supersedes_baseline_id is None:
            return "baseline_supersession_required" if approved_v2 else None
        if (
            len(current) != 1
            or current[0].consequence_id != baseline.supersedes_baseline_id
        ):
            return "baseline_supersession_invalid"
        if current[0].consequence.task_text is not None and consequence.task_text is None:
            return "task_text_required"
        return None

    async def propose(self, consequence: HumanReviewConsequence) -> HumanReviewResult:
        """Persist non-authoritative typed intent for one exact canonical package revision."""
        try:
            async with self.engine.begin() as connection:
                reason = await self._package_reason(
                    connection, consequence.package_work_id, consequence.package_revision
                )
                if reason is not None:
                    return HumanReviewResult(
                        status="DENIED" if reason == "package_not_found" else "STALE",
                        reason=reason,
                    )
                rows = await self._proposals(
                    connection, consequence.package_work_id, consequence.package_revision
                )
                if len(rows) > 1:
                    return HumanReviewResult(status="UNKNOWN", reason="state_unavailable")
                if rows:
                    existing = human_review_record(rows[0])
                    if existing.consequence_digest != consequence.digest:
                        return HumanReviewResult(status="CONFLICT", reason="consequence_changed")
                    return HumanReviewResult(status="REPLAYED", record=existing)
                supersession_reason = await self._baseline_supersession_reason(
                    connection, consequence
                )
                if supersession_reason is not None:
                    return HumanReviewResult(status="DENIED", reason=supersession_reason)
                values = {
                    "consequence_id": consequence.consequence_id,
                    "package_work_id": consequence.package_work_id,
                    "package_revision": consequence.package_revision,
                    "consequence_digest": consequence.digest,
                    "consequence": consequence.model_dump(mode="json"),
                    "state": "PENDING",
                }
                row = (await connection.execute(
                    insert(human_review_consequences).values(values).returning(
                        *human_review_consequences.c
                    )
                )).mappings().one()
                return HumanReviewResult(status="PREPARED", record=human_review_record(row))
        except (SQLAlchemyError, TypeError, ValueError):
            return HumanReviewResult(status="UNKNOWN", reason="state_unavailable")

    async def prepare(
        self, package_work_id: UUID, package_revision: str
    ) -> HumanReviewResult:
        try:
            async with self.engine.begin() as connection:
                reason = await self._package_reason(
                    connection, package_work_id, package_revision
                )
                if reason is not None:
                    return HumanReviewResult(
                        status="STALE" if reason != "package_not_found" else "DENIED",
                        reason=reason,
                    )
                rows = await self._proposals(connection, package_work_id, package_revision)
                if not rows:
                    return HumanReviewResult(status="UNKNOWN", reason="consequence_unavailable")
                if len(rows) > 1:
                    return HumanReviewResult(status="UNKNOWN", reason="state_unavailable")
                return HumanReviewResult(status="REPLAYED", record=human_review_record(rows[0]))
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
                reason = await self._package_reason(
                    connection, package_work_id, package_revision
                )
                if reason is not None:
                    return HumanReviewResult(status="STALE", reason=reason)
                rows = await self._proposals(connection, package_work_id, package_revision)
                if not rows:
                    return HumanReviewResult(status="DENIED", reason="consequence_unavailable")
                if len(rows) > 1:
                    return HumanReviewResult(status="UNKNOWN", reason="state_unavailable")
                row = rows[0]
                if (
                    row["consequence_id"] != consequence_id
                    or row["package_work_id"] != package_work_id
                    or row["package_revision"] != package_revision
                ):
                    return HumanReviewResult(status="STALE", reason="consequence_changed")
                if row["decision"] is not None:
                    if row["decision"] != decision:
                        return HumanReviewResult(status="CONFLICT", reason="decision_conflict")
                    return HumanReviewResult(status="REPLAYED", record=human_review_record(row))
                if decision == "APPROVED" and human_review_record(row).consequence.task_text is not None:
                    package = await self.works.get_locked(connection, package_work_id)
                    if package is None or len(package.notes.encode("utf-8")) > 2048:
                        return HumanReviewResult(status="DENIED", reason="agent_notes_overflow")
                state = "READY_FOR_IMPLEMENTATION" if decision == "APPROVED" else decision
                decided = (await connection.execute(update(human_review_consequences).where(
                    human_review_consequences.c.consequence_id == consequence_id
                ).values(decision=decision, state=state, decided_at=func.now()).returning(
                    *human_review_consequences.c
                ))).mappings().one()
                return HumanReviewResult(status="RECORDED", record=human_review_record(decided))
        except (SQLAlchemyError, TypeError, ValueError):
            return HumanReviewResult(status="UNKNOWN", reason="state_unavailable")
