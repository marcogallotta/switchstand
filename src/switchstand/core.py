from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from .contracts import (
    AppendResult,
    GroupedLookup,
    LaunchAuthority,
    RelatedLookup,
    Routing,
    SourceStoriesRequest,
    SourceStoriesResult,
    SourceStory,
    SourceStoryRequest,
    SourceStoryResult,
    SourceTask,
    SourceTaskRequest,
    SourceTaskResult,
    SuggestionResult,
    WorkAppendRequest,
    WorkAttachment,
    WorkAttachmentsRequest,
    WorkAttachmentsResult,
    WorkEvent,
    WorkEventRequest,
    WorkEventResult,
    WorkGetRequest,
    WorkHead,
    WorkHistoryRequest,
    WorkHistoryResult,
    WorkItem,
    WorkPatch,
    WorkResult,
    WorkSource,
    WorkUpdateRequest,
)


class ProviderError(Exception):
    """A provider failure whose details must not cross the controller boundary."""


class UnknownEffect(ProviderError):
    """A single external effect was sent but its outcome is unknown."""


@dataclass(frozen=True)
class Handle:
    id: UUID
    provider: str
    provider_work_id: str


@dataclass(frozen=True)
class EventBinding:
    id: UUID
    work_id: UUID
    provider: str
    provider_work_id: str
    provider_event_id: str


@dataclass(frozen=True)
class ProviderWork:
    title: str
    notes: str
    completed: bool
    revision: str
    routing: Routing
    canonical: bool


@dataclass(frozen=True)
class ProviderHead:
    provider_work_id: str
    title: str
    priority: str
    horizon: str | None


@dataclass(frozen=True)
class ProviderSourceTask:
    title: str
    notes: str
    completed: bool
    revision: str
    canonical: bool


@dataclass(frozen=True)
class ProviderSourceStory:
    story_gid: str
    task_gid: str
    subtype: str | None
    text: str | None
    created_at: str
    created_by: str | None


@dataclass(frozen=True)
class ProviderStoriesPage:
    task_gid: str
    revision: str
    stories: tuple[ProviderSourceStory, ...]
    next_offset: str | None
    canonical: bool
    stale: bool = False


@dataclass(frozen=True)
class ProviderAttachment:
    name: str


@dataclass(frozen=True)
class AttachmentPage:
    attachments: tuple[ProviderAttachment, ...]
    next_cursor: str | None


class State(Protocol):
    async def get(self, work_id: UUID) -> Handle | None: ...
    async def bound_provider_ids(self, provider: str) -> frozenset[str]: ...
    def locked(self, work_id: UUID) -> AbstractAsyncContextManager[Handle | None]: ...
    async def bind(self, provider: str, provider_work_id: str) -> Handle: ...


class EventState(Protocol):
    async def bind_event(
        self, work_id: UUID, provider: str, provider_work_id: str, provider_event_id: str,
    ) -> EventBinding: ...
    async def get_event(self, work_id: UUID, event_id: UUID) -> EventBinding | None: ...


class Provider(Protocol):
    async def get(self, provider_work_id: str) -> ProviderWork | None: ...
    async def list_attachments(
        self, provider_work_id: str, cursor: str | None, limit: int
    ) -> AttachmentPage: ...
    async def find_related(self, work_task_gid: str) -> RelatedLookup: ...
    async def find_grouped(self, root_task_gid: str) -> GroupedLookup: ...
    async def update(self, provider_work_id: str, patch: WorkPatch) -> None: ...
    async def append(self, provider_work_id: str, text: str) -> str | None: ...
    async def suggest_next(self, excluded: frozenset[str]) -> ProviderHead | None: ...
    async def source_task(self, provider_task_id: str) -> ProviderSourceTask | None: ...
    async def source_stories(
        self, provider_task_id: str, observed_revision: str, offset: str | None, limit: int
    ) -> ProviderStoriesPage | None: ...
    async def source_story(
        self, provider_task_id: str, provider_story_id: str
    ) -> ProviderSourceStory | None: ...


async def apply_scalar(
    provider: Provider, provider_work_id: str, patch: WorkPatch, *, send: bool = True,
) -> ProviderWork | None:
    """Apply at most once, then accept only canonical converged provider state."""
    if send:
        await provider.update(provider_work_id, patch)
    try:
        work = await provider.get(provider_work_id)
    except ProviderError:
        if send:
            raise UnknownEffect("update readback unavailable") from None
        raise
    routing = {"priority", "horizon", "review_next_action", "stage3_gate"}
    matches = work is not None and all(
        getattr(work.routing if field in routing else work, field) == getattr(patch, field)
        for field in patch.model_fields_set
    )
    return work if work is not None and work.canonical and matches else None


class Controller:
    def __init__(self, authority: LaunchAuthority, state: State, providers: dict[str, Provider]):
        self.authority, self.state, self.providers = authority, state, providers

    def _item(self, work_id: UUID, work: ProviderWork, handle: Handle) -> WorkItem:
        return WorkItem(
            id=work_id, title=work.title, notes=work.notes, completed=work.completed,
            revision=work.revision, routing=work.routing,
            source=WorkSource(provider=handle.provider, task_gid=handle.provider_work_id),
        )

    @staticmethod
    def _source_story(story: ProviderSourceStory) -> SourceStory:
        return SourceStory(
            story_gid=story.story_gid,
            task_gid=story.task_gid,
            subtype=story.subtype,
            text=story.text,
            created_at=story.created_at,
            created_by=story.created_by,
        )

    @staticmethod
    def _work_event(
        work_id: UUID, event_id: UUID, story: ProviderSourceStory,
    ) -> WorkEvent:
        return WorkEvent(
            id=event_id,
            work_id=work_id,
            subtype=story.subtype,
            text=story.text,
            created_at=story.created_at,
            actor=story.created_by,
        )

    async def _read(self, work_id: UUID, handle: Handle) -> WorkResult:
        provider = self.providers.get(handle.provider)
        if provider is None:
            return WorkResult(status="provider_error")
        work = await provider.get(handle.provider_work_id)
        if work is None:
            return WorkResult(status="unknown")
        if not work.canonical:
            return WorkResult(status="denied")
        return WorkResult(status="ok", item=self._item(work_id, work, handle))

    def _source_provider(self) -> Provider | None:
        return self.providers.get("asana")

    async def bind_work(self, provider_name: str, provider_work_id: str) -> Handle:
        provider = self.providers[provider_name]
        work = await provider.get(provider_work_id)
        if work is None or not work.canonical:
            raise PermissionError("work is not canonical")
        return await self.state.bind(provider_name, provider_work_id)

    async def get(self, request: WorkGetRequest) -> WorkResult:
        if not self.authority.can_read(request.work_id):
            return WorkResult(status="denied")
        try:
            handle = await self.state.get(request.work_id)
            if handle is None:
                return WorkResult(status="unknown")
            result = await self._read(request.work_id, handle)
            if not request.include_related or result.status != "ok" or result.item is None:
                return result
            related = RelatedLookup(status="UH_OH", work_task_gid=handle.provider_work_id,
                                    reason="provider_not_supported")
            if handle.provider == "asana":
                try:
                    related = await self.providers[handle.provider].find_related(handle.provider_work_id)
                except ProviderError:
                    related = RelatedLookup(status="UH_OH", work_task_gid=handle.provider_work_id,
                                            reason="related_unavailable")
                if related.reason == "work_not_canonical":
                    return WorkResult(status="denied")
                if (related.work_task_gid != handle.provider_work_id
                        or related.observed_revision != result.item.revision):
                    related = RelatedLookup(status="UH_OH", work_task_gid=handle.provider_work_id,
                                            observed_revision=related.observed_revision,
                                            reason="work_revision_mismatch")
            grouped = GroupedLookup(status="UH_OH", root_task_gid=handle.provider_work_id,
                                    reason="provider_not_supported")
            if handle.provider == "asana" and hasattr(self.providers[handle.provider], "find_grouped"):
                try:
                    grouped = await self.providers[handle.provider].find_grouped(handle.provider_work_id)
                except ProviderError:
                    grouped = GroupedLookup(status="UH_OH", root_task_gid=handle.provider_work_id,
                                            reason="grouped_unavailable")
                if grouped.reason == "work_not_canonical":
                    return WorkResult(status="denied")
                if (grouped.root_task_gid != handle.provider_work_id
                        or (grouped.status == "CANDIDATES"
                            or grouped.observed_revision is not None)
                        and grouped.observed_revision != result.item.revision):
                    grouped = GroupedLookup(status="UH_OH", root_task_gid=handle.provider_work_id,
                                            observed_revision=grouped.observed_revision,
                                            reason="work_revision_mismatch")
            return WorkResult(status="ok", item=result.item, related=related, grouped=grouped)
        except UnknownEffect:
            return WorkResult(status="unknown")
        except ProviderError:
            return WorkResult(status="provider_error")

    async def attachments(self, request: WorkAttachmentsRequest) -> WorkAttachmentsResult:
        if not self.authority.can_read(request.work_id):
            return WorkAttachmentsResult(status="denied")
        try:
            handle = await self.state.get(request.work_id)
            if handle is None:
                return WorkAttachmentsResult(status="unknown")
            provider = self.providers.get(handle.provider)
            if provider is None:
                return WorkAttachmentsResult(status="provider_error")
            before = await provider.get(handle.provider_work_id)
            if before is None:
                return WorkAttachmentsResult(status="unknown")
            if not before.canonical:
                return WorkAttachmentsResult(status="denied")
            if before.revision != request.observed_revision:
                return WorkAttachmentsResult(
                    status="stale", work_id=request.work_id, revision=before.revision
                )
            page = await provider.list_attachments(
                handle.provider_work_id, request.cursor, request.limit
            )
            after = await provider.get(handle.provider_work_id)
            if after is None:
                return WorkAttachmentsResult(status="unknown")
            if not after.canonical:
                return WorkAttachmentsResult(status="denied")
            if after.revision != before.revision:
                return WorkAttachmentsResult(
                    status="stale", work_id=request.work_id, revision=after.revision
                )
            return WorkAttachmentsResult(
                status="ok", work_id=request.work_id, revision=after.revision,
                attachments=tuple(WorkAttachment(name=item.name) for item in page.attachments),
                next_cursor=page.next_cursor,
            )
        except UnknownEffect:
            return WorkAttachmentsResult(status="unknown")
        except ProviderError:
            return WorkAttachmentsResult(status="provider_error")

    async def history(self, request: WorkHistoryRequest) -> WorkHistoryResult:
        if not self.authority.can_read(request.work_id):
            return WorkHistoryResult(status="denied")
        try:
            handle = await self.state.get(request.work_id)
            if handle is None:
                return WorkHistoryResult(status="unknown")
            provider = self.providers.get(handle.provider)
            if provider is None:
                return WorkHistoryResult(status="provider_error")
            page = await provider.source_stories(
                handle.provider_work_id, request.observed_revision, request.cursor, request.limit
            )
            if page is None:
                return WorkHistoryResult(status="unknown")
            if not page.canonical:
                return WorkHistoryResult(status="denied")
            if (page.task_gid != handle.provider_work_id or len(page.stories) > request.limit
                    or any(story.task_gid != handle.provider_work_id for story in page.stories)):
                return WorkHistoryResult(status="provider_error")
            if page.stale or page.revision != request.observed_revision:
                return WorkHistoryResult(
                    status="stale", work_id=request.work_id, revision=page.revision
                )
            event_state = cast(EventState, self.state)
            events: list[WorkEvent] = []
            for story in page.stories:
                binding = await event_state.bind_event(
                    request.work_id, handle.provider, handle.provider_work_id, story.story_gid
                )
                events.append(self._work_event(request.work_id, binding.id, story))
            return WorkHistoryResult(
                status="ok",
                work_id=request.work_id,
                revision=page.revision,
                events=tuple(events),
                next_cursor=page.next_offset,
            )
        except UnknownEffect:
            return WorkHistoryResult(status="unknown")
        except ProviderError:
            return WorkHistoryResult(status="provider_error")

    async def event(self, request: WorkEventRequest) -> WorkEventResult:
        if not self.authority.can_read(request.work_id):
            return WorkEventResult(status="denied")
        try:
            handle = await self.state.get(request.work_id)
            if handle is None:
                return WorkEventResult(status="unknown")
            event_state = cast(EventState, self.state)
            binding = await event_state.get_event(request.work_id, request.event_id)
            if (binding is None or binding.provider != handle.provider
                    or binding.provider_work_id != handle.provider_work_id):
                return WorkEventResult(status="denied")
            provider_event_id = binding.provider_event_id
            provider = self.providers.get(handle.provider)
            if provider is None:
                return WorkEventResult(status="provider_error")
            before = await provider.source_task(handle.provider_work_id)
            if before is None:
                return WorkEventResult(status="unknown")
            if not before.canonical:
                return WorkEventResult(status="denied")
            if before.revision != request.observed_revision:
                return WorkEventResult(
                    status="stale", work_id=request.work_id, revision=before.revision
                )
            story = await provider.source_story(handle.provider_work_id, provider_event_id)
            if story is None:
                return WorkEventResult(status="unknown")
            if (story.task_gid != handle.provider_work_id
                    or story.story_gid != provider_event_id):
                return WorkEventResult(status="denied")
            after = await provider.source_task(handle.provider_work_id)
            if after is None:
                return WorkEventResult(status="unknown")
            if not after.canonical:
                return WorkEventResult(status="denied")
            if after.revision != before.revision:
                return WorkEventResult(
                    status="stale", work_id=request.work_id, revision=after.revision
                )
            return WorkEventResult(
                status="ok",
                work_id=request.work_id,
                revision=after.revision,
                item=self._work_event(request.work_id, request.event_id, story),
            )
        except UnknownEffect:
            return WorkEventResult(status="unknown")
        except ProviderError:
            return WorkEventResult(status="provider_error")

    async def source_task(self, request: SourceTaskRequest) -> SourceTaskResult:
        provider = self._source_provider()
        if provider is None:
            return SourceTaskResult(status="provider_error")
        try:
            task = await provider.source_task(request.task_gid)
            if task is None:
                return SourceTaskResult(status="unknown")
            if not task.canonical:
                return SourceTaskResult(status="denied")
            return SourceTaskResult(
                status="ok",
                item=SourceTask(
                    task_gid=request.task_gid,
                    title=task.title,
                    notes=task.notes,
                    completed=task.completed,
                    revision=task.revision,
                ),
            )
        except UnknownEffect:
            return SourceTaskResult(status="unknown")
        except ProviderError:
            return SourceTaskResult(status="provider_error")

    async def source_stories(self, request: SourceStoriesRequest) -> SourceStoriesResult:
        provider = self._source_provider()
        if provider is None:
            return SourceStoriesResult(status="provider_error")
        try:
            page = await provider.source_stories(
                request.task_gid, request.observed_revision, request.offset, request.limit
            )
            if page is None:
                return SourceStoriesResult(status="unknown")
            if not page.canonical:
                return SourceStoriesResult(status="denied")
            if (page.task_gid != request.task_gid or len(page.stories) > request.limit
                    or any(story.task_gid != request.task_gid for story in page.stories)):
                return SourceStoriesResult(status="provider_error")
            if page.stale or page.revision != request.observed_revision:
                return SourceStoriesResult(
                    status="stale", task_gid=page.task_gid, revision=page.revision
                )
            return SourceStoriesResult(
                status="ok",
                task_gid=page.task_gid,
                revision=page.revision,
                stories=tuple(self._source_story(story) for story in page.stories),
                next_offset=page.next_offset,
            )
        except UnknownEffect:
            return SourceStoriesResult(status="unknown")
        except ProviderError:
            return SourceStoriesResult(status="provider_error")

    async def source_story(self, request: SourceStoryRequest) -> SourceStoryResult:
        provider = self._source_provider()
        if provider is None:
            return SourceStoryResult(status="provider_error")
        try:
            before = await provider.source_task(request.task_gid)
            if before is None:
                return SourceStoryResult(status="unknown")
            if not before.canonical:
                return SourceStoryResult(status="denied")
            if before.revision != request.observed_revision:
                return SourceStoryResult(
                    status="stale", task_gid=request.task_gid, revision=before.revision
                )
            story = await provider.source_story(request.task_gid, request.story_gid)
            if story is None:
                return SourceStoryResult(status="unknown")
            if story.task_gid != request.task_gid or story.story_gid != request.story_gid:
                return SourceStoryResult(status="denied")
            after = await provider.source_task(request.task_gid)
            if after is None:
                return SourceStoryResult(status="unknown")
            if not after.canonical:
                return SourceStoryResult(status="denied")
            if after.revision != before.revision:
                return SourceStoryResult(
                    status="stale", task_gid=request.task_gid, revision=after.revision
                )
            return SourceStoryResult(
                status="ok",
                task_gid=request.task_gid,
                revision=after.revision,
                item=self._source_story(story),
            )
        except UnknownEffect:
            return SourceStoryResult(status="unknown")
        except ProviderError:
            return SourceStoryResult(status="provider_error")

    async def update(self, request: WorkUpdateRequest) -> WorkResult:
        if request.work_id != self.authority.active_work_id:
            return WorkResult(status="denied")
        update_may_have_applied = False
        try:
            async with self.state.locked(request.work_id) as handle:
                if handle is None:
                    return WorkResult(status="unknown")
                current = await self._read(request.work_id, handle)
                if current.status != "ok" or current.item is None:
                    return current
                if current.item.revision != request.observed_revision:
                    return WorkResult(status="stale", item=current.item)
                try:
                    work = await apply_scalar(
                        self.providers[handle.provider], handle.provider_work_id, request.patch,
                    )
                except UnknownEffect:
                    update_may_have_applied = True
                    return WorkResult(status="unknown")
                update_may_have_applied = True
                if work is None:
                    return WorkResult(status="unknown")
                return WorkResult(status="ok", item=self._item(request.work_id, work, handle))
        except UnknownEffect:
            return WorkResult(status="unknown")
        except ProviderError:
            return WorkResult(status="unknown" if update_may_have_applied else "provider_error")
        except Exception:
            if update_may_have_applied:
                return WorkResult(status="unknown")
            raise

    async def append(self, request: WorkAppendRequest) -> AppendResult:
        if request.work_id != self.authority.active_work_id:
            return AppendResult(status="denied")
        append_returned = False
        try:
            async with self.state.locked(request.work_id) as handle:
                if handle is None:
                    return AppendResult(status="unknown")
                current = await self._read(request.work_id, handle)
                if current.status != "ok":
                    return AppendResult(status=current.status if current.status != "stale" else "provider_error")
                provider = self.providers[handle.provider]
                story_gid = await provider.append(handle.provider_work_id, request.text)
                append_returned = True
                if story_gid is None:
                    return AppendResult(status="unknown")
                story = await provider.source_story(handle.provider_work_id, story_gid)
                if (story is None or story.story_gid != story_gid
                        or story.task_gid != handle.provider_work_id or story.text != request.text):
                    return AppendResult(status="unknown")
                readback = await self._read(request.work_id, handle)
                if readback.status != "ok":
                    return AppendResult(status="unknown")
                return AppendResult(
                    status="ok", task_gid=handle.provider_work_id, story_gid=story_gid
                )
        except UnknownEffect:
            return AppendResult(status="unknown")
        except ProviderError:
            return AppendResult(status="unknown" if append_returned else "provider_error")
        except Exception:
            if append_returned:
                return AppendResult(status="unknown")
            raise

    async def suggest_next(self) -> SuggestionResult:
        try:
            active = await self.state.get(self.authority.active_work_id)
            if active is None or (provider := self.providers.get(active.provider)) is None:
                return SuggestionResult(status="provider_error")
            excluded = await self.state.bound_provider_ids(active.provider)
            candidate = await provider.suggest_next(excluded)
            if candidate is None:
                return SuggestionResult(status="none")
            handle = await self.state.bind(active.provider, candidate.provider_work_id)
            return SuggestionResult(status="ok", item=WorkHead(
                id=handle.id, title=candidate.title, priority=candidate.priority,
                horizon=candidate.horizon,
            ))
        except (ProviderError, UnknownEffect):
            return SuggestionResult(status="provider_error")


async def provision_launch(
    state: State,
    provider_name: str,
    provider: Provider,
    active_provider_work_id: str,
    reference_provider_work_ids: tuple[str, ...] = (),
) -> LaunchAuthority:
    provider_work_ids = (active_provider_work_id, *reference_provider_work_ids)
    if len(reference_provider_work_ids) > 8:
        raise ValueError("at most eight reference tasks are allowed")
    if len(set(provider_work_ids)) != len(provider_work_ids):
        raise ValueError("active and reference tasks must be distinct")

    works = [await provider.get(provider_work_id) for provider_work_id in provider_work_ids]
    if any(work is None or not work.canonical for work in works):
        raise PermissionError("all work must be canonical")

    handles = [await state.bind(provider_name, provider_work_id) for provider_work_id in provider_work_ids]
    return LaunchAuthority(
        active_work_id=handles[0].id,
        reference_work_ids=tuple(handle.id for handle in handles[1:]),
    )
