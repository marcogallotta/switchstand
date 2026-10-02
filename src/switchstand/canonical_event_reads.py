"""Inert DB-only projection for work history and exact event reads."""

from __future__ import annotations

from uuid import UUID

from .canonical_work import CanonicalWorkRepository, canonical_revision
from .contracts import (
    WorkEvent,
    WorkEventRequest,
    WorkEventResult,
    WorkHistoryRequest,
    WorkHistoryResult,
)
from .work_events import StoredWorkEvent, WorkEventRepository


def _public_event(event: StoredWorkEvent) -> WorkEvent:
    return WorkEvent(
        id=event.id,
        work_id=event.work_id,
        subtype=event.subtype,
        text=event.text,
        created_at=event.created_at.isoformat(),
        actor=event.actor,
    )


class CanonicalEventReader:
    """Project canonical event storage through the existing public read contracts."""

    def __init__(self, works: CanonicalWorkRepository, events: WorkEventRepository):
        self.works = works
        self.events = events

    async def _revision(self, work_id: UUID) -> str | None:
        work = await self.works.get(work_id)
        return None if work is None else canonical_revision(work_id, work.row_version)

    async def history(self, request: WorkHistoryRequest) -> WorkHistoryResult:
        revision = await self._revision(request.work_id)
        if revision is None:
            return WorkHistoryResult(status="unknown")
        if request.observed_revision != revision:
            return WorkHistoryResult(
                status="stale", work_id=request.work_id, revision=revision,
            )
        page = await self.events.page(
            request.work_id, cursor=request.cursor, limit=request.limit,
        )
        latest = await self._revision(request.work_id)
        if latest is None:
            return WorkHistoryResult(status="unknown")
        if latest != revision:
            return WorkHistoryResult(
                status="stale", work_id=request.work_id, revision=latest,
            )
        return WorkHistoryResult(
            status="ok",
            work_id=request.work_id,
            revision=revision,
            events=tuple(_public_event(event) for event in page.events),
            next_cursor=page.next_cursor,
        )

    async def event(self, request: WorkEventRequest) -> WorkEventResult:
        revision = await self._revision(request.work_id)
        if revision is None:
            return WorkEventResult(status="unknown")
        if request.observed_revision != revision:
            return WorkEventResult(
                status="stale", work_id=request.work_id, revision=revision,
            )
        event = await self.events.get(request.work_id, request.event_id)
        latest = await self._revision(request.work_id)
        if latest is None:
            return WorkEventResult(status="unknown")
        if latest != revision:
            return WorkEventResult(
                status="stale", work_id=request.work_id, revision=latest,
            )
        if event is None:
            return WorkEventResult(status="unknown")
        return WorkEventResult(
            status="ok", work_id=request.work_id, revision=revision,
            item=_public_event(event),
        )
