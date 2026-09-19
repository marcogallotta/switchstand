from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

ApiVersion = Literal["1"]
AsanaGid = str
Status = Literal["ok", "stale", "denied", "unknown", "provider_error"]
SourceStatus = Literal["ok", "stale", "denied", "unknown", "provider_error"]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Routing(ClosedModel):
    priority: str | None = None
    work_type: str | None = None
    horizon: str | None = None
    review_next_action: str | None = None
    stage3_gate: str | None = None


class WorkSource(ClosedModel):
    provider: str
    task_gid: str


class WorkItem(ClosedModel):
    id: UUID
    title: str
    notes: str
    completed: bool
    revision: str
    routing: Routing
    source: WorkSource | None = None


class WorkPatch(ClosedModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    notes: str | None = None
    completed: bool | None = None
    priority: str | None = Field(default=None, min_length=1)
    work_type: str | None = Field(default=None, min_length=1)
    horizon: str | None = None
    review_next_action: str | None = None
    stage3_gate: str | None = None

    @model_validator(mode="after")
    def valid_patch(self) -> Self:
        changed = self.model_fields_set
        if not changed:
            raise ValueError("patch must not be empty")
        if any(getattr(self, field) is None for field in changed):
            raise ValueError("patch values must not be null")
        if changed & {"horizon", "review_next_action", "stage3_gate"} and "notes" not in changed:
            raise ValueError("routing changes require notes")
        return self


class WorkSearchRequest(ClosedModel):
    api_version: ApiVersion
    text: str | None = Field(default=None, min_length=1, max_length=500)
    completed: bool | None = None
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)
    limit: int = Field(default=50, ge=1, le=100)


class WorkSearchItem(ClosedModel):
    id: UUID
    title: str
    completed: bool
    revision: str
    routing: Routing


class WorkSearchResult(ClosedModel):
    status: Literal["ok", "denied", "unknown", "provider_error"]
    items: tuple[WorkSearchItem, ...] = ()
    next_cursor: str | None = None

    @model_validator(mode="after")
    def valid_result(self) -> Self:
        if self.status != "ok" and (self.items or self.next_cursor is not None):
            raise ValueError("failed work search must not claim result data")
        return self


class WorkGetRequest(ClosedModel):
    api_version: ApiVersion
    work_id: UUID
    include_related: bool = False


class WorkUpdateRequest(ClosedModel):
    api_version: ApiVersion
    work_id: UUID
    observed_revision: str
    patch: WorkPatch


class WorkAppendRequest(ClosedModel):
    api_version: ApiVersion
    work_id: UUID
    text: str = Field(min_length=1)


class RelatedCandidate(ClosedModel):
    task_gid: AsanaGid
    title: str
    revision: str
    parent_gid: AsanaGid
    work_type_option_gid: AsanaGid | None = None


class RelatedLookup(ClosedModel):
    status: Literal["CANDIDATES", "UH_OH"]
    work_task_gid: AsanaGid
    observed_revision: str | None = None
    candidates: tuple[RelatedCandidate, ...] = ()
    reason: str | None = None


class GroupedCandidate(ClosedModel):
    task_gid: AsanaGid
    title: str
    revision: str
    root_work_gid: AsanaGid
    source: Literal["asana_root_work_gid_search_exact_get"]


class GroupedLookup(ClosedModel):
    status: Literal["CANDIDATES", "UH_OH"]
    root_task_gid: AsanaGid
    observed_revision: str | None = None
    candidates: tuple[GroupedCandidate, ...] = ()
    complete: Literal[False] = False
    reason: str | None = None


class WorkResult(ClosedModel):
    status: Status
    item: WorkItem | None = None
    related: RelatedLookup | None = None
    grouped: GroupedLookup | None = None

    @model_validator(mode="after")
    def valid_result(self) -> Self:
        needs_item = self.status in {"ok", "stale"}
        if needs_item != (self.item is not None):
            raise ValueError("item presence does not match status")
        if (self.related is not None or self.grouped is not None) and self.status != "ok":
            raise ValueError("related evidence requires current readable work")
        return self


class AppendResult(ClosedModel):
    status: Literal["ok", "denied", "unknown", "provider_error"]
    task_gid: str | None = None
    story_gid: str | None = None

    @model_validator(mode="after")
    def valid_result(self) -> Self:
        has_identity = self.task_gid is not None or self.story_gid is not None
        if self.status == "ok":
            if self.task_gid is None or self.story_gid is None:
                raise ValueError("successful append requires exact target and story identity")
        elif has_identity:
            raise ValueError("failed append must not claim effect identity")
        return self


class WorkHead(ClosedModel):
    id: UUID
    title: str
    priority: str
    horizon: str | None = None


class SuggestionResult(ClosedModel):
    status: Literal["ok", "none", "provider_error"]
    item: WorkHead | None = None

    @model_validator(mode="after")
    def valid_result(self) -> Self:
        if (self.status == "ok") != (self.item is not None):
            raise ValueError("item presence does not match status")
        return self


class LaunchAuthority(ClosedModel):
    active_work_id: UUID
    reference_work_ids: tuple[UUID, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.active_work_id in self.reference_work_ids or len(set(self.reference_work_ids)) != len(self.reference_work_ids):
            raise ValueError("launch handles must be distinct")
        return self

    def can_read(self, work_id: UUID) -> bool:
        return work_id == self.active_work_id or work_id in self.reference_work_ids


class SourceTask(ClosedModel):
    task_gid: AsanaGid
    title: str
    notes: str
    completed: bool
    revision: str


class SourceStory(ClosedModel):
    story_gid: AsanaGid
    task_gid: AsanaGid
    subtype: str | None = None
    text: str | None = None
    created_at: str
    created_by: str | None = None


class SourceTaskRequest(ClosedModel):
    api_version: ApiVersion
    task_gid: AsanaGid = Field(min_length=1, pattern=r"^[0-9]+$")


class SourceStoriesRequest(ClosedModel):
    api_version: ApiVersion
    task_gid: AsanaGid = Field(min_length=1, pattern=r"^[0-9]+$")
    observed_revision: str = Field(min_length=1)
    offset: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class SourceStoryRequest(ClosedModel):
    api_version: ApiVersion
    task_gid: AsanaGid = Field(min_length=1, pattern=r"^[0-9]+$")
    story_gid: AsanaGid = Field(min_length=1, pattern=r"^[0-9]+$")
    observed_revision: str = Field(min_length=1)


class SourceTaskResult(ClosedModel):
    status: Literal["ok", "denied", "unknown", "provider_error"]
    item: SourceTask | None = None

    @model_validator(mode="after")
    def valid_result(self) -> Self:
        if (self.status == "ok") != (self.item is not None):
            raise ValueError("item presence does not match status")
        return self


class SourceStoriesResult(ClosedModel):
    status: SourceStatus
    task_gid: AsanaGid | None = None
    revision: str | None = None
    stories: tuple[SourceStory, ...] = ()
    next_offset: str | None = None

    @model_validator(mode="after")
    def valid_result(self) -> Self:
        if self.status == "ok":
            if self.task_gid is None or self.revision is None:
                raise ValueError("successful source page requires target and revision")
        elif self.status == "stale":
            if self.task_gid is None or self.revision is None or self.stories or self.next_offset is not None:
                raise ValueError("stale source page requires only current target and revision")
        elif self.task_gid is not None or self.revision is not None or self.stories or self.next_offset is not None:
            raise ValueError("failed source page must not claim source data")
        return self


class SourceStoryResult(ClosedModel):
    status: SourceStatus
    task_gid: AsanaGid | None = None
    revision: str | None = None
    item: SourceStory | None = None

    @model_validator(mode="after")
    def valid_result(self) -> Self:
        if self.status == "ok":
            if self.task_gid is None or self.revision is None or self.item is None:
                raise ValueError("successful story reread requires target, revision and story")
        elif self.status == "stale":
            if self.task_gid is None or self.revision is None or self.item is not None:
                raise ValueError("stale story reread requires current target and revision only")
        elif self.task_gid is not None or self.revision is not None or self.item is not None:
            raise ValueError("failed story reread must not claim source data")
        return self


class WorkEvent(ClosedModel):
    id: UUID
    work_id: UUID
    subtype: str | None = None
    text: str | None = None
    created_at: str
    actor: str | None = None


class WorkHistoryRequest(ClosedModel):
    api_version: ApiVersion
    work_id: UUID
    observed_revision: str = Field(min_length=1)
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class WorkEventRequest(ClosedModel):
    api_version: ApiVersion
    work_id: UUID
    event_id: UUID
    observed_revision: str = Field(min_length=1)


class WorkHistoryResult(ClosedModel):
    status: Status
    work_id: UUID | None = None
    revision: str | None = None
    events: tuple[WorkEvent, ...] = ()
    next_cursor: str | None = None

    @model_validator(mode="after")
    def valid_result(self) -> Self:
        if self.status == "ok":
            if self.work_id is None or self.revision is None:
                raise ValueError("successful work history requires work and revision")
            if any(event.work_id != self.work_id for event in self.events):
                raise ValueError("history events must belong to the requested work")
        elif self.status == "stale":
            if self.work_id is None or self.revision is None or self.events or self.next_cursor is not None:
                raise ValueError("stale work history requires only current work and revision")
        elif self.work_id is not None or self.revision is not None or self.events or self.next_cursor is not None:
            raise ValueError("failed work history must not claim event data")
        return self


class WorkEventResult(ClosedModel):
    status: Status
    work_id: UUID | None = None
    revision: str | None = None
    item: WorkEvent | None = None

    @model_validator(mode="after")
    def valid_result(self) -> Self:
        if self.status == "ok":
            if self.work_id is None or self.revision is None or self.item is None:
                raise ValueError("successful event reread requires work, revision and event")
            if self.item.work_id != self.work_id:
                raise ValueError("event must belong to the requested work")
        elif self.status == "stale":
            if self.work_id is None or self.revision is None or self.item is not None:
                raise ValueError("stale event reread requires current work and revision only")
        elif self.work_id is not None or self.revision is not None or self.item is not None:
            raise ValueError("failed event reread must not claim event data")
        return self
