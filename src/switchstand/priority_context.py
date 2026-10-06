from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from .canonical_relations import CanonicalRelationsRepository, WorkRelations
from .canonical_work import CanonicalWorkRepository, CurrentWork
from .contracts import ClosedModel
from .priority_claim_service import (
    PriorityClaimReadResult,
    PriorityClaimService,
    PriorityClaimView,
)

PriorityKnowledge = Literal[
    "DIRECT_CURRENT",
    "DERIVED_CURRENT",
    "CONFLICTING_OR_STALE",
    "UNKNOWN_UNRANKED",
]


class PriorityContextClaim(PriorityClaimView):
    authority: Literal["HUMAN", "ADVISORY"]


class PriorityContextProject(ClosedModel):
    project_id: UUID
    project_name: str
    stage: str


class PriorityContextRow(ClosedModel):
    work_id: UUID
    state: Literal["CURRENT", "UNKNOWN"]
    title: str
    completed: bool | None = None
    owner_key: str
    lifecycle_state: str
    projects: tuple[PriorityContextProject, ...] = ()
    next_action_class: str
    next_action_ref: str
    wait_kind: str
    unblock_condition: str
    dependency_work_ids: tuple[UUID, ...] = ()
    work_claims: tuple[PriorityContextClaim, ...] = ()
    project_claims: tuple[PriorityContextClaim, ...] = ()
    priority_knowledge: PriorityKnowledge
    flags: tuple[str, ...] = ()
    coarse_size: Literal["UNKNOWN"] = "UNKNOWN"
    headline: Literal["UNKNOWN"] = "UNKNOWN"
    human_attention: Literal["UNKNOWN"] = "UNKNOWN"


class PriorityContextResult(ClosedModel):
    status: Literal["ok", "denied", "unknown"]
    scope: Literal["EXACT_WORK_IDS"] = "EXACT_WORK_IDS"
    scope_complete: bool
    portfolio_complete: Literal[False] = False
    next_cursor: None = None
    rows: tuple[PriorityContextRow, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class _BaseRow:
    work_id: UUID
    work: CurrentWork | None
    relations: WorkRelations | None
    error: str = ""


class PriorityContextProjection:
    """Build a bounded, read-only priority context without ranking work."""

    def __init__(
        self,
        *,
        works: CanonicalWorkRepository,
        relations: CanonicalRelationsRepository,
        claims: PriorityClaimService,
    ) -> None:
        self._works = works
        self._relations = relations
        self._claims = claims

    async def project(self, work_ids: tuple[UUID, ...]) -> PriorityContextResult:
        ordered_ids = tuple(sorted(set(work_ids), key=str))
        if not ordered_ids or len(ordered_ids) > 50:
            raise ValueError("work_ids must contain between 1 and 50 distinct IDs")

        bases = tuple([await self._load_base(work_id) for work_id in ordered_ids])
        project_ids = {
            placement.project_id
            for base in bases
            if base.relations is not None
            for placement in base.relations.placements
        }
        project_claims = {
            project_id: await self._claims.current("PROJECT", project_id)
            for project_id in sorted(project_ids, key=str)
        }
        rows = tuple(
            [await self._project_row(base, set(ordered_ids), project_ids, project_claims) for base in bases]
        )
        return PriorityContextResult(
            status="ok",
            scope_complete=True,
            rows=rows,
            reason="Exact requested IDs only; this is not a complete portfolio view.",
        )

    async def _load_base(self, work_id: UUID) -> _BaseRow:
        try:
            work = await self._works.get(work_id)
            relations = await self._relations.get(work_id)
        except (LookupError, SQLAlchemyError) as exc:
            return _BaseRow(work_id=work_id, work=None, relations=None, error=type(exc).__name__)
        return _BaseRow(work_id=work_id, work=work, relations=relations)

    async def _project_row(
        self,
        base: _BaseRow,
        work_scope: set[UUID],
        project_scope: set[UUID],
        project_reads: dict[UUID, PriorityClaimReadResult],
    ) -> PriorityContextRow:
        if base.work is None or base.relations is None:
            return self._unknown_row(base.work_id, base.error)

        work_read = await self._claims.current("WORK", base.work_id)
        work_claims = self._visible_claims(work_read, work_scope)
        project_claims = tuple(
            claim
            for placement in base.relations.placements
            for claim in self._visible_claims(project_reads[placement.project_id], project_scope)
        )
        reads = (work_read, *(project_reads[p.project_id] for p in base.relations.placements))
        flags = self._flags(work_claims, project_claims, reads)
        classification = self._classification(work_claims, work_read)
        work = base.work
        return PriorityContextRow(
            work_id=base.work_id,
            state="CURRENT",
            title=work.title,
            completed=work.completed,
            owner_key=work.owner_key or "UNKNOWN",
            lifecycle_state=work.lifecycle_state or "UNKNOWN",
            projects=tuple(
                PriorityContextProject(
                    project_id=placement.project_id,
                    project_name=placement.name,
                    stage=placement.section_name or "UNKNOWN",
                )
                for placement in base.relations.placements
            ),
            next_action_class=work.next_action_class or "UNKNOWN",
            next_action_ref=work.next_action_ref or "UNKNOWN",
            wait_kind=work.wait_kind or "UNKNOWN",
            unblock_condition=work.unblock_condition or "UNKNOWN",
            dependency_work_ids=base.relations.dependency_work_ids,
            work_claims=work_claims,
            project_claims=project_claims,
            priority_knowledge=classification,
            flags=flags,
        )

    @staticmethod
    def _visible_claims(
        read: PriorityClaimReadResult, target_scope: set[UUID]
    ) -> tuple[PriorityContextClaim, ...]:
        return tuple(
            PriorityContextClaim(
                **claim.model_dump(),
                authority="HUMAN" if claim.claim_kind == "HUMAN_PRIORITY" else "ADVISORY",
            )
            for claim in read.claims
            if claim.relation_kind != "BEFORE" or claim.relation_target_id in target_scope
        )

    @staticmethod
    def _flags(
        work_claims: tuple[PriorityContextClaim, ...],
        project_claims: tuple[PriorityContextClaim, ...],
        reads: tuple[PriorityClaimReadResult, ...],
    ) -> tuple[str, ...]:
        flags: set[str] = set()
        if any(read.status != "ok" for read in reads):
            flags.add("CLAIMS_UNKNOWN")
        if any(claim.currentness == "STALE" for claim in (*work_claims, *project_claims)):
            flags.add("STALE_CLAIM")
        if any(claim.currentness == "UNKNOWN" for claim in (*work_claims, *project_claims)):
            flags.add("UNKNOWN_CLAIM_CURRENTNESS")
        human_bands = {
            claim.band
            for claim in work_claims
            if claim.authority == "HUMAN"
            and claim.relation_kind == "BAND"
            and claim.currentness == "CURRENT"
        }
        if len(human_bands) > 1:
            flags.add("CONFLICTING_HUMAN_BAND")
        project_subjects = {claim.subject_id for claim in project_claims}
        if any(len({
            claim.band for claim in project_claims
            if claim.subject_id == subject_id and claim.authority == "HUMAN"
            and claim.relation_kind == "BAND" and claim.currentness == "CURRENT"
        }) > 1 for subject_id in project_subjects):
            flags.add("CONFLICTING_PROJECT_HUMAN_BAND")
        return tuple(sorted(flags))

    @staticmethod
    def _classification(
        work_claims: tuple[PriorityContextClaim, ...],
        work_read: PriorityClaimReadResult,
    ) -> PriorityKnowledge:
        flags = PriorityContextProjection._flags(work_claims, (), (work_read,))
        if "CONFLICTING_HUMAN_BAND" in flags or "STALE_CLAIM" in flags:
            return "CONFLICTING_OR_STALE"
        if "CLAIMS_UNKNOWN" in flags or "UNKNOWN_CLAIM_CURRENTNESS" in flags:
            return "UNKNOWN_UNRANKED"
        if any(claim.authority == "HUMAN" for claim in work_claims):
            return "DIRECT_CURRENT"
        if work_claims:
            return "DERIVED_CURRENT"
        return "UNKNOWN_UNRANKED"

    @staticmethod
    def _unknown_row(work_id: UUID, error: str) -> PriorityContextRow:
        return PriorityContextRow(
            work_id=work_id,
            state="UNKNOWN",
            title="UNKNOWN",
            owner_key="UNKNOWN",
            lifecycle_state="UNKNOWN",
            next_action_class="UNKNOWN",
            next_action_ref="UNKNOWN",
            wait_kind="UNKNOWN",
            unblock_condition="UNKNOWN",
            priority_knowledge="UNKNOWN_UNRANKED",
            flags=("WORK_UNKNOWN",),
        )
