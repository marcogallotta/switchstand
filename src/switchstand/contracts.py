from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

ApiVersion = Literal["1"]
Status = Literal["ok", "stale", "denied", "unknown", "provider_error"]

class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Routing(ClosedModel):
    priority: str | None = None
    horizon: str | None = None
    review_next_action: str | None = None
    stage3_gate: str | None = None

class WorkItem(ClosedModel):
    id: UUID
    title: str
    notes: str
    completed: bool
    revision: str
    routing: Routing

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
