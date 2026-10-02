"""Inert DB-only projection for current work reads, search, and scalar updates."""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol, cast
from uuid import UUID

from .canonical_relations import CanonicalRelationsRepository, WorkRelations
from .canonical_work import CanonicalWorkRepository, CurrentWork, canonical_revision
from .contracts import (
    Routing,
    WorkContext,
    WorkItem,
    WorkPatch,
    WorkPlacement,
    WorkResult,
    WorkSearchItem,
    WorkSearchRequest,
    WorkSearchResult,
    WorkUpdateRequest,
)

_SCALAR_FIELDS = frozenset({
    "title", "notes", "completed", "priority", "work_type", "lifecycle_state",
    "review_next_action", "wait_kind", "unblock_condition", "next_due",
})


class _TypedUpdateRequest(Protocol):
    patch: WorkPatch


def _routing(work: CurrentWork) -> Routing:
    return Routing(
        priority=work.priority,
        work_type=work.work_type,
        lifecycle_state=work.lifecycle_state,
        review_next_action=work.review_next_action,
        wait_kind=work.wait_kind,
        unblock_condition=work.unblock_condition,
        next_due=work.next_due,
    )


def _context(work: CurrentWork, relations: WorkRelations) -> WorkContext:
    return WorkContext(
        assignee=work.assignee,
        placements=tuple(
            WorkPlacement(area=value.name, stage=value.section_name)
            for value in relations.placements
        ),
    )


class CanonicalWorkRuntime:
    """Project compact canonical storage through existing public work contracts."""

    def __init__(
        self, works: CanonicalWorkRepository, relations: CanonicalRelationsRepository,
    ):
        self.works = works
        self.relations = relations

    async def _item(self, work: CurrentWork) -> WorkItem | None:
        relations = await self.relations.get(work.work_id)
        if await self.works.get(work.work_id) != work:
            return None
        return WorkItem(
            id=work.work_id,
            title=work.title,
            notes=work.notes,
            completed=work.completed,
            revision=canonical_revision(work.work_id, work.row_version),
            routing=_routing(work),
            context=_context(work, relations),
        )

    async def get(self, work_id: UUID) -> WorkResult:
        for _ in range(2):
            work = await self.works.get(work_id)
            if work is None:
                return WorkResult(status="unknown")
            item = await self._item(work)
            if item is not None:
                return WorkResult(status="ok", item=item)
        return WorkResult(status="unknown")

    async def search(self, request: WorkSearchRequest) -> WorkSearchResult:
        for _ in range(2):
            page = await self.works.search(
                request.text,
                completed=request.completed,
                cursor=request.cursor,
                limit=request.limit,
            )
            items: list[WorkSearchItem] = []
            for work in page.items:
                public = await self._item(work)
                if public is None:
                    break
                items.append(WorkSearchItem(
                    id=public.id,
                    title=public.title,
                    completed=public.completed,
                    revision=public.revision,
                    routing=public.routing,
                    context=public.context,
                ))
            else:
                return WorkSearchResult(
                    status="ok", items=tuple(items), next_cursor=page.next_cursor,
                )
        return WorkSearchResult(status="unknown")

    async def update(self, request: WorkUpdateRequest) -> WorkResult:
        current = await self.works.get(request.work_id)
        current_item = None if current is None else await self._item(current)
        if current is None or current_item is None:
            return WorkResult(status="unknown")
        if request.observed_revision != current_item.revision:
            return WorkResult(status="stale", item=current_item)
        patch: WorkPatch = cast(_TypedUpdateRequest, request).patch  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        fields = patch.model_fields_set
        if not fields <= _SCALAR_FIELDS:
            return WorkResult(status="denied")
        values = {field: getattr(patch, field) for field in fields}  # pyright: ignore[reportUnknownArgumentType]
        try:
            changed = await self.works.replace(replace(current, **values))
        except ValueError as error:
            if "stale canonical work version" not in str(error):
                return WorkResult(status="denied")
            latest = await self.works.get(request.work_id)
            latest_item = None if latest is None else await self._item(latest)
            return (
                WorkResult(status="unknown") if latest_item is None
                else WorkResult(status="stale", item=latest_item)
            )
        changed_item = await self._item(changed)
        if changed_item is None:
            return WorkResult(status="unknown")
        return WorkResult(status="ok", item=changed_item)
