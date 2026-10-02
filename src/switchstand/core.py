import hashlib
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import Literal, Protocol, cast
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from .contracts import (
    AppendResult,
    LaunchAuthority,
    Routing,
    SourceStoriesRequest,
    SourceStoriesResult,
    SourceStory,
    SourceStoryRequest,
    SourceStoryResult,
    SourceTask,
    SourceTaskRequest,
    SourceTaskResult,
    WorkAppendRequest,
    WorkAttachment,
    WorkAttachmentsRequest,
    WorkAttachmentsResult,
    WorkContext,
    WorkEvent,
    WorkEventRequest,
    WorkEventResult,
    WorkGetRequest,
    WorkHistoryRequest,
    WorkHistoryResult,
    WorkItem,
    WorkPatch,
    WorkResult,
    WorkSource,
    WorkUpdateRequest,
)

ProviderFailure = Literal[
    "invalid_request",
    "admission_denied",
    "authority_denied",
    "transient",
    "permanent",
    "unspecified",
]


class ProviderError(Exception):
    """A provider failure whose raw details must not cross the controller boundary."""

    def __init__(self, message: str, *, failure: ProviderFailure = "unspecified"):
        super().__init__(message)
        self.failure = failure


def provider_rejection_reason(error: ProviderError) -> str:
    """Return a closed, provider-neutral reason without exposing raw provider detail."""
    return {
        "invalid_request": "provider_rejected_input",
        "admission_denied": "provider_admission_denied",
        "authority_denied": "provider_authority_denied",
        "transient": "provider_temporarily_unavailable",
        "permanent": "provider_permanent_rejection",
        "unspecified": "provider_rejected_send",
    }[error.failure]


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
class ProviderRelation:
    kind: str
    action: str
    target_gid: str | None = None
    assignee_gid: str | None = None
    project_gid: str | None = None
    section_gid: str | None = None


@dataclass(frozen=True)
class ProviderWork:
    title: str
    notes: str
    completed: bool
    revision: str
    routing: Routing
    context: WorkContext
    canonical: bool


@dataclass(frozen=True)
class ProviderSourceTask:
    title: str
    notes: str
    completed: bool
    revision: str
    context: WorkContext
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
    async def get_by_provider(self, provider: str, provider_work_id: str) -> Handle | None: ...
    def locked(self, work_id: UUID) -> AbstractAsyncContextManager[Handle | None]: ...
    async def bind(self, provider: str, provider_work_id: str) -> Handle: ...
    async def bind_many(
        self, provider: str, provider_work_ids: tuple[str, ...]
    ) -> tuple[Handle, ...]: ...


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
    async def update(self, provider_work_id: str, patch: WorkPatch) -> None: ...
    async def append(self, provider_work_id: str, text: str) -> str | None: ...
    async def source_task(self, provider_task_id: str) -> ProviderSourceTask | None: ...
    async def source_stories(
        self, provider_task_id: str, observed_revision: str, offset: str | None, limit: int,
        *, require_canonical: bool = True,
    ) -> ProviderStoriesPage | None: ...
    async def source_story(
        self, provider_task_id: str, provider_story_id: str
    ) -> ProviderSourceStory | None: ...


async def authoritative_revision(
    state: object, work_id: UUID, provider_revision: str, *,
    provider_notes: str | None = None, provider_context: WorkContext | None = None,
) -> str:
    index = getattr(state, "work_index", None)
    revision = provider_revision if index is None else await index.revision(
        work_id, provider_revision, provider_notes=provider_notes,
        provider_context=provider_context,
    )
    token = await content_authorization_token(state, work_id)
    return revision_with_authorization(revision, token)


def revision_with_authorization(revision: str, token: str | None) -> str:
    """Compose the public revision from its DB authority components."""
    if token is None:
        return revision
    return "s3_" + hashlib.sha256(f"{token}\0{revision}".encode()).hexdigest()


async def content_authorization_token(state: object, work_id: UUID) -> str | None:
    """Return DB content authority after Stage 3; ``None`` preserves provider authority."""
    worksets = getattr(state, "worksets", None)
    if worksets is None:
        return None
    return cast(str | None, await worksets.content_authorization(work_id))


async def authorize_content(
    state: object, work_id: UUID, provider_canonical: bool,
) -> str | None:
    """Authorize exact provider content from the current sole authority."""
    token = await content_authorization_token(state, work_id)
    if token is None and not provider_canonical:
        raise PermissionError("provider does not admit content")
    return token


def authorization_stable(before: str | None, after: str | None) -> bool:
    return before == after


async def observed_revision_matches(
    state: object, work_id: UUID, observed: str, provider_revision: str, *,
    provider_notes: str | None = None, provider_context: WorkContext | None = None,
) -> bool:
    return observed == await authoritative_revision(
        state, work_id, provider_revision, provider_notes=provider_notes,
        provider_context=provider_context,
    )


async def authoritative_work(
    state: object, work_id: UUID, provider: ProviderWork,
) -> ProviderWork:
    index = getattr(state, "work_index", None)
    projected = provider if index is None else await index.project(work_id, provider)
    token = await content_authorization_token(state, work_id)
    if token is None:
        return projected
    revision = "s3_" + hashlib.sha256(f"{token}\0{projected.revision}".encode()).hexdigest()
    return replace(projected, revision=revision)


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
    routing = {"priority", "work_type", "horizon", "review_next_action", "stage3_gate"}
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
            revision=work.revision, routing=work.routing, context=work.context,
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
        try:
            prior_authorization = await content_authorization_token(self.state, work_id)
        except PermissionError:
            return WorkResult(status="denied")
        work = await provider.get(handle.provider_work_id)
        if work is None:
            return WorkResult(status="unknown")
        try:
            authorization = await authorize_content(self.state, work_id, work.canonical)
        except PermissionError:
            return WorkResult(status="denied")
        try:
            projected = await authoritative_work(self.state, work_id, work)
            if not authorization_stable(prior_authorization, authorization):
                return WorkResult(status="stale", item=self._item(work_id, projected, handle))
            index = getattr(self.state, "work_index", None)
            if index is not None and await index.active():
                after = await provider.get(handle.provider_work_id)
                if after is None:
                    return WorkResult(status="unknown")
                try:
                    latest_authorization = await authorize_content(
                        self.state, work_id, after.canonical
                    )
                except PermissionError:
                    return WorkResult(status="denied")
                stable = await authoritative_work(self.state, work_id, after)
                if (not authorization_stable(authorization, latest_authorization)
                        or after.revision != work.revision
                        or stable.revision != projected.revision):
                    return WorkResult(status="stale", item=self._item(work_id, stable, handle))
                projected = stable
        except PermissionError:
            return WorkResult(status="denied")
        return WorkResult(status="ok", item=self._item(work_id, projected, handle))

    def _source_provider(self) -> Provider | None:
        return self.providers.get("asana")

    async def _source_authorization(self, provider_task_id: str) -> tuple[UUID, str] | None:
        """Resolve legacy provider content through DB membership only after Stage 3."""
        worksets = getattr(self.state, "worksets", None)
        if worksets is None or await worksets.generation() is None:
            return None
        handle = await self.state.get_by_provider("asana", provider_task_id)
        if handle is None:
            raise PermissionError("provider task is not admitted")
        token = await content_authorization_token(self.state, handle.id)
        assert token is not None
        return handle.id, token

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
            return await self._read(request.work_id, handle)
        except UnknownEffect:
            return WorkResult(status="unknown")
        except ProviderError:
            return WorkResult(status="provider_error")
        except (SQLAlchemyError, ValueError, KeyError):
            return WorkResult(status="unknown")

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
            try:
                authorization = await authorize_content(
                    self.state, request.work_id, before.canonical
                )
            except PermissionError:
                return WorkAttachmentsResult(status="denied")
            current_revision = await authoritative_revision(
                self.state, request.work_id, before.revision,
                provider_notes=before.notes, provider_context=before.context,
            )
            if current_revision != request.observed_revision:
                return WorkAttachmentsResult(
                    status="stale", work_id=request.work_id, revision=current_revision
                )
            page = await provider.list_attachments(
                handle.provider_work_id, request.cursor, request.limit
            )
            after = await provider.get(handle.provider_work_id)
            if after is None:
                return WorkAttachmentsResult(status="unknown")
            try:
                latest_authorization = await authorize_content(
                    self.state, request.work_id, after.canonical
                )
            except PermissionError:
                return WorkAttachmentsResult(status="denied")
            resulting_revision = await authoritative_revision(
                self.state, request.work_id, after.revision,
                provider_notes=after.notes, provider_context=after.context,
            )
            if (not authorization_stable(authorization, latest_authorization)
                    or resulting_revision != current_revision):
                return WorkAttachmentsResult(
                    status="stale", work_id=request.work_id,
                    revision=resulting_revision,
                )
            return WorkAttachmentsResult(
                status="ok", work_id=request.work_id, revision=resulting_revision,
                attachments=tuple(WorkAttachment(name=item.name) for item in page.attachments),
                next_cursor=page.next_cursor,
            )
        except UnknownEffect:
            return WorkAttachmentsResult(status="unknown")
        except ProviderError:
            return WorkAttachmentsResult(status="provider_error")
        except (SQLAlchemyError, ValueError, KeyError):
            return WorkAttachmentsResult(status="unknown")

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
            before = await provider.source_task(handle.provider_work_id)
            if before is None:
                return WorkHistoryResult(status="unknown")
            try:
                authorization = await authorize_content(
                    self.state, request.work_id, before.canonical
                )
            except PermissionError:
                return WorkHistoryResult(status="denied")
            current_revision = await authoritative_revision(
                self.state, request.work_id, before.revision,
                provider_notes=before.notes, provider_context=before.context,
            )
            if current_revision != request.observed_revision:
                return WorkHistoryResult(
                    status="stale", work_id=request.work_id, revision=current_revision
                )
            page = await provider.source_stories(
                handle.provider_work_id, before.revision, request.cursor, request.limit,
                require_canonical=authorization is None,
            )
            if page is None:
                return WorkHistoryResult(status="unknown")
            if authorization is None and not page.canonical:
                return WorkHistoryResult(status="denied")
            if (page.task_gid != handle.provider_work_id or len(page.stories) > request.limit
                    or any(story.task_gid != handle.provider_work_id for story in page.stories)):
                return WorkHistoryResult(status="provider_error")
            if page.stale or page.revision != before.revision:
                return WorkHistoryResult(
                    status="stale", work_id=request.work_id,
                    revision=await authoritative_revision(
                        self.state, request.work_id, page.revision,
                        provider_notes=before.notes, provider_context=before.context,
                    ),
                )
            after = await provider.source_task(handle.provider_work_id)
            if after is None:
                return WorkHistoryResult(status="unknown")
            try:
                latest_authorization = await authorize_content(
                    self.state, request.work_id, after.canonical
                )
            except PermissionError:
                return WorkHistoryResult(status="denied")
            resulting_revision = await authoritative_revision(
                self.state, request.work_id, after.revision,
                provider_notes=after.notes, provider_context=after.context,
            )
            if (not authorization_stable(authorization, latest_authorization)
                    or resulting_revision != current_revision):
                return WorkHistoryResult(
                    status="stale", work_id=request.work_id,
                    revision=resulting_revision,
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
                revision=resulting_revision,
                events=tuple(events),
                next_cursor=page.next_offset,
            )
        except UnknownEffect:
            return WorkHistoryResult(status="unknown")
        except ProviderError:
            return WorkHistoryResult(status="provider_error")
        except (SQLAlchemyError, ValueError, KeyError):
            return WorkHistoryResult(status="unknown")

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
            try:
                authorization = await authorize_content(
                    self.state, request.work_id, before.canonical
                )
            except PermissionError:
                return WorkEventResult(status="denied")
            current_revision = await authoritative_revision(
                self.state, request.work_id, before.revision,
                provider_notes=before.notes, provider_context=before.context,
            )
            if current_revision != request.observed_revision:
                return WorkEventResult(
                    status="stale", work_id=request.work_id, revision=current_revision
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
            try:
                latest_authorization = await authorize_content(
                    self.state, request.work_id, after.canonical
                )
            except PermissionError:
                return WorkEventResult(status="denied")
            resulting_revision = await authoritative_revision(
                self.state, request.work_id, after.revision,
                provider_notes=after.notes, provider_context=after.context,
            )
            if (not authorization_stable(authorization, latest_authorization)
                    or resulting_revision != current_revision):
                return WorkEventResult(
                    status="stale", work_id=request.work_id,
                    revision=resulting_revision,
                )
            return WorkEventResult(
                status="ok",
                work_id=request.work_id,
                revision=resulting_revision,
                item=self._work_event(request.work_id, request.event_id, story),
            )
        except UnknownEffect:
            return WorkEventResult(status="unknown")
        except ProviderError:
            return WorkEventResult(status="provider_error")
        except (SQLAlchemyError, ValueError, KeyError):
            return WorkEventResult(status="unknown")

    async def source_task(self, request: SourceTaskRequest) -> SourceTaskResult:
        provider = self._source_provider()
        if provider is None:
            return SourceTaskResult(status="provider_error")
        try:
            authorization = await self._source_authorization(request.task_gid)
            task = await provider.source_task(request.task_gid)
            if task is None:
                return SourceTaskResult(status="unknown")
            latest_authorization = await self._source_authorization(request.task_gid)
            if authorization != latest_authorization:
                return SourceTaskResult(status="unknown")
            if authorization is None and not task.canonical:
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
        except PermissionError:
            return SourceTaskResult(status="denied")
        except ProviderError:
            return SourceTaskResult(status="provider_error")
        except (SQLAlchemyError, ValueError, KeyError):
            return SourceTaskResult(status="unknown")

    async def source_stories(self, request: SourceStoriesRequest) -> SourceStoriesResult:
        provider = self._source_provider()
        if provider is None:
            return SourceStoriesResult(status="provider_error")
        try:
            authorization = await self._source_authorization(request.task_gid)
            page = await provider.source_stories(
                request.task_gid, request.observed_revision, request.offset, request.limit,
                require_canonical=authorization is None,
            )
            if page is None:
                return SourceStoriesResult(status="unknown")
            latest_authorization = await self._source_authorization(request.task_gid)
            if authorization != latest_authorization:
                return SourceStoriesResult(
                    status="stale", task_gid=request.task_gid, revision=page.revision
                )
            if authorization is None and not page.canonical:
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
        except PermissionError:
            return SourceStoriesResult(status="denied")
        except ProviderError:
            return SourceStoriesResult(status="provider_error")
        except (SQLAlchemyError, ValueError, KeyError):
            return SourceStoriesResult(status="unknown")

    async def source_story(self, request: SourceStoryRequest) -> SourceStoryResult:
        provider = self._source_provider()
        if provider is None:
            return SourceStoryResult(status="provider_error")
        try:
            authorization = await self._source_authorization(request.task_gid)
            before = await provider.source_task(request.task_gid)
            if before is None:
                return SourceStoryResult(status="unknown")
            if authorization is None and not before.canonical:
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
            latest_authorization = await self._source_authorization(request.task_gid)
            if authorization != latest_authorization:
                return SourceStoryResult(
                    status="stale", task_gid=request.task_gid, revision=after.revision
                )
            if authorization is None and not after.canonical:
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
        except PermissionError:
            return SourceStoryResult(status="denied")
        except ProviderError:
            return SourceStoryResult(status="provider_error")
        except (SQLAlchemyError, ValueError, KeyError):
            return SourceStoryResult(status="unknown")

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
