from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

ApiVersion = Literal["1"]
Status = Literal["ok", "stale", "denied", "unknown", "provider_error"]
SourceStatus = Literal["ok", "stale", "denied", "unknown", "provider_error"]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Routing(ClosedModel):
    priority: str | None = None
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
    notes: str | None = None
    completed: bool | None = None
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


class WorkGetRequest(ClosedModel):
    api_version: ApiVersion
    work_id: UUID


class WorkUpdateRequest(ClosedModel):
    api_version: ApiVersion
    work_id: UUID
    observed_revision: str
    patch: WorkPatch


class WorkAppendRequest(ClosedModel):
    api_version: ApiVersion
    work_id: UUID
    text: str = Field(min_length=1)


class WorkResult(ClosedModel):
    status: Status
    item: WorkItem | None = None

    @model_validator(mode="after")
    def valid_result(self) -> Self:
        needs_item = self.status in {"ok", "stale"}
        if needs_item != (self.item is not None):
            raise ValueError("item presence does not match status")
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


AsanaGid = str


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
