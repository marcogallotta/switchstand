"""Inert durable priority-claim occurrences and bounded reads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Table,
    Text,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .canonical_relations import projects
from .canonical_work import canonical_metadata, canonical_revision, canonical_work

ClaimKind = Literal["HUMAN_PRIORITY", "AGENT_RECOMMENDATION"]
SubjectKind = Literal["WORK", "PROJECT"]
RelationKind = Literal["BAND", "BEFORE", "HOLD"]
PriorityBand = Literal["HIGH", "NORMAL"]


class StalePriorityClaim(ValueError):
    pass


priority_claims = Table(
    "priority_claims", canonical_metadata,
    Column("claim_id", PGUUID(as_uuid=True), primary_key=True),
    Column("claim_kind", Text, nullable=False),
    Column("subject_kind", Text, nullable=False),
    Column("subject_id", PGUUID(as_uuid=True), nullable=False),
    Column("relation_kind", Text, nullable=False),
    Column("relation_target_id", PGUUID(as_uuid=True)),
    Column("band", Text),
    Column("rationale", Text, nullable=False),
    Column("source_label", Text, nullable=False),
    Column("source_ref", Text, nullable=False),
    Column("source_observed_revision", Text),
    Column("state", Text, nullable=False),
    Column(
        "supersedes_claim_id", PGUUID(as_uuid=True),
        ForeignKey("priority_claims.claim_id", ondelete="RESTRICT"), unique=True,
    ),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    CheckConstraint(
        "claim_kind IN ('HUMAN_PRIORITY', 'AGENT_RECOMMENDATION')",
        name="ck_priority_claim_kind",
    ),
    CheckConstraint("subject_kind IN ('WORK', 'PROJECT')", name="ck_priority_subject_kind"),
    CheckConstraint(
        "relation_kind IN ('BAND', 'BEFORE', 'HOLD')", name="ck_priority_relation_kind",
    ),
    CheckConstraint("state IN ('CURRENT', 'SUPERSEDED')", name="ck_priority_state"),
    CheckConstraint(
        "(relation_kind = 'BAND' AND band IS NOT NULL AND band IN ('HIGH', 'NORMAL') "
        "AND relation_target_id IS NULL) OR "
        "(relation_kind = 'BEFORE' AND band IS NULL AND relation_target_id IS NOT NULL) OR "
        "(relation_kind = 'HOLD' AND band IS NULL AND relation_target_id IS NULL)",
        name="ck_priority_relation_shape",
    ),
    CheckConstraint("length(rationale) BETWEEN 1 AND 300", name="ck_priority_rationale"),
    CheckConstraint("length(source_label) BETWEEN 1 AND 512", name="ck_priority_source_label"),
    CheckConstraint("length(source_ref) BETWEEN 1 AND 512", name="ck_priority_source_ref"),
    CheckConstraint(
        "source_observed_revision IS NULL OR "
        "length(source_observed_revision) BETWEEN 1 AND 512",
        name="ck_priority_source_revision",
    ),
    CheckConstraint(
        "supersedes_claim_id IS NULL OR supersedes_claim_id <> claim_id",
        name="ck_priority_supersedes_not_self",
    ),
)
Index(
    "ix_priority_claims_current_subject", priority_claims.c.subject_kind,
    priority_claims.c.subject_id, priority_claims.c.created_at,
    postgresql_where=priority_claims.c.state == "CURRENT",
)
@dataclass(frozen=True)
class NewPriorityClaim:
    claim_id: UUID
    claim_kind: ClaimKind
    subject_kind: SubjectKind
    subject_id: UUID
    relation_kind: RelationKind
    rationale: str
    source_label: str
    source_ref: str
    relation_target_id: UUID | None = None
    band: PriorityBand | None = None
    source_observed_revision: str | None = None
    supersedes_claim_id: UUID | None = None
@dataclass(frozen=True)
class PriorityClaim(NewPriorityClaim):
    state: str = "CURRENT"
    created_at: datetime | None = None
_COLUMNS = tuple(priority_claims.c)
def _claim(row: tuple[object, ...]) -> PriorityClaim:
    return PriorityClaim(
        claim_id=cast(UUID, row[0]), claim_kind=cast(ClaimKind, row[1]),
        subject_kind=cast(SubjectKind, row[2]), subject_id=cast(UUID, row[3]),
        relation_kind=cast(RelationKind, row[4]),
        relation_target_id=cast(UUID | None, row[5]),
        band=cast(PriorityBand | None, row[6]),
        rationale=cast(str, row[7]), source_label=cast(str, row[8]),
        source_ref=cast(str, row[9]), source_observed_revision=cast(str | None, row[10]),
        state=cast(str, row[11]), supersedes_claim_id=cast(UUID | None, row[12]),
        created_at=cast(datetime, row[13]),
    )
class PriorityClaimRepository:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine
    @staticmethod
    def _subject_table(kind: SubjectKind) -> tuple[Table, Column[UUID]]:
        return ((canonical_work, canonical_work.c.work_id) if kind == "WORK"
                else (projects, projects.c.project_id))
    @staticmethod
    async def _require_subject(
        connection: AsyncConnection, kind: SubjectKind, subject_id: UUID, *, lock: bool = False,
    ) -> None:
        table, column = PriorityClaimRepository._subject_table(kind)
        statement = select(column).select_from(table).where(column == subject_id)
        if lock:
            statement = statement.with_for_update()
        if await connection.scalar(statement) is None:
            raise LookupError(f"{kind.lower()} subject does not exist")
    @staticmethod
    async def _require_revision(
        connection: AsyncConnection, kind: SubjectKind, subject_id: UUID,
        observed_revision: str | None,
    ) -> None:
        if kind != "WORK" or observed_revision is None:
            return
        row = (await connection.execute(select(
            canonical_work.c.work_id, canonical_work.c.row_version,
        ).where(canonical_work.c.work_id == subject_id).with_for_update())).one_or_none()
        if row is None:
            raise LookupError("work subject does not exist")
        if observed_revision != canonical_revision(row[0], row[1]):
            raise StalePriorityClaim("source revision changed")
    async def record(
        self, value: NewPriorityClaim, *, observed_revision: str | None = None,
    ) -> PriorityClaim:
        if value.claim_id == value.supersedes_claim_id:
            raise ValueError("priority claim cannot supersede itself")
        async with self.engine.begin() as connection:
            await self._require_subject(connection, value.subject_kind, value.subject_id, lock=True)
            await self._require_revision(
                connection, value.subject_kind, value.subject_id, observed_revision,
            )
            if value.relation_kind == "BEFORE":
                if value.relation_target_id == value.subject_id:
                    raise ValueError("priority claim cannot precede itself")
                if value.relation_target_id is not None:
                    await self._require_subject(
                        connection, value.subject_kind, value.relation_target_id,
                    )
            if value.supersedes_claim_id is not None:
                predecessor = (await connection.execute(select(*_COLUMNS).where(
                    priority_claims.c.claim_id == value.supersedes_claim_id
                ).with_for_update())).one_or_none()
                if predecessor is None:
                    raise LookupError("superseded priority claim does not exist")
                old = _claim(tuple(predecessor))
                if (
                    old.state != "CURRENT" or old.claim_kind != value.claim_kind
                    or old.subject_kind != value.subject_kind
                    or old.subject_id != value.subject_id
                ):
                    raise ValueError("superseded priority claim is not current in this scope")
                await connection.execute(update(priority_claims).where(
                    priority_claims.c.claim_id == old.claim_id
                ).values(state="SUPERSEDED"))
            row = (await connection.execute(insert(priority_claims).values(
                **value.__dict__, state="CURRENT",
            ).returning(*_COLUMNS))).one()
        return _claim(tuple(row))
    async def get(self, claim_id: UUID) -> PriorityClaim | None:
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(*_COLUMNS).where(
                priority_claims.c.claim_id == claim_id
            ))).one_or_none()
        return None if row is None else _claim(tuple(row))
    async def clear(
        self, value: NewPriorityClaim, *, observed_revision: str | None = None,
    ) -> PriorityClaim:
        """Atomically supersede one exact HUMAN claim and retain a non-current tombstone."""
        if value.supersedes_claim_id is None or value.claim_id == value.supersedes_claim_id:
            raise ValueError("priority clear requires a distinct target claim")
        async with self.engine.begin() as connection:
            replay = (await connection.execute(select(*_COLUMNS).where(
                priority_claims.c.claim_id == value.claim_id
            ).with_for_update())).one_or_none()
            if replay is not None:
                return _claim(tuple(replay))
            await self._require_subject(
                connection, value.subject_kind, value.subject_id, lock=True,
            )
            await self._require_revision(
                connection, value.subject_kind, value.subject_id, observed_revision,
            )
            target = (await connection.execute(select(*_COLUMNS).where(
                priority_claims.c.claim_id == value.supersedes_claim_id
            ).with_for_update())).one_or_none()
            if target is None:
                raise LookupError("cleared priority claim does not exist")
            old = _claim(tuple(target))
            if (
                old.state != "CURRENT" or old.claim_kind != "HUMAN_PRIORITY"
                or old.subject_kind != value.subject_kind
                or old.subject_id != value.subject_id
            ):
                raise ValueError("cleared priority claim is not current HUMAN state in scope")
            await connection.execute(update(priority_claims).where(
                priority_claims.c.claim_id == old.claim_id
            ).values(state="SUPERSEDED"))
            row = (await connection.execute(insert(priority_claims).values(
                **value.__dict__, state="SUPERSEDED",
            ).returning(*_COLUMNS))).one()
        return _claim(tuple(row))
    async def current(self, kind: SubjectKind, subject_id: UUID) -> tuple[PriorityClaim, ...]:
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(*_COLUMNS).where(
                (priority_claims.c.subject_kind == kind)
                & (priority_claims.c.subject_id == subject_id)
                & (priority_claims.c.state == "CURRENT")
            ).order_by(priority_claims.c.created_at, priority_claims.c.claim_id))).all()
        return tuple(_claim(tuple(row)) for row in rows)
    async def before_edges(
        self, kind: SubjectKind, subject_ids: tuple[UUID, ...], *, limit: int = 500,
    ) -> tuple[PriorityClaim, ...]:
        if limit < 1 or limit > 500 or len(subject_ids) > 500:
            raise ValueError("priority claim scope and limit must be between 1 and 500")
        if not subject_ids:
            return ()
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(*_COLUMNS).where(
                (priority_claims.c.subject_kind == kind)
                & priority_claims.c.subject_id.in_(subject_ids)
                & priority_claims.c.relation_target_id.in_(subject_ids)
                & (priority_claims.c.relation_kind == "BEFORE")
                & (priority_claims.c.state == "CURRENT")
            ).order_by(priority_claims.c.created_at, priority_claims.c.claim_id).limit(limit))).all()
        return tuple(_claim(tuple(row)) for row in rows)
    async def provenance(self, claim_id: UUID, *, limit: int = 100) -> tuple[PriorityClaim, ...]:
        if limit < 1 or limit > 500:
            raise ValueError("priority claim provenance limit must be between 1 and 500")
        result: list[PriorityClaim] = []
        async with self.engine.connect() as connection:
            current: UUID | None = claim_id
            while current is not None and len(result) < limit:
                row = (await connection.execute(select(*_COLUMNS).where(
                    priority_claims.c.claim_id == current
                ))).one_or_none()
                if row is None:
                    break
                claim = _claim(tuple(row))
                result.append(claim)
                current = claim.supersedes_claim_id
        return tuple(result)
