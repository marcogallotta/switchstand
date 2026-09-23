"""Trusted caller/grant values. None of the issuance inputs are MCP arguments."""

import hashlib
from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from .contracts import ClosedModel, LaunchAuthority, RelatedLookup, WorkItem


class PrincipalContext(ClosedModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    assurance: Literal["test", "authenticated"]

    @property
    def key(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class WorkGrant(ClosedModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    version: int = Field(ge=1)
    principal: PrincipalContext
    authority: LaunchAuthority
    scope: Literal["launch", "workspace"] = "launch"
    operations: frozenset[Literal[
        "work_get", "work_search", "work_append", "work_create", "work_update", "message"
    ]]
    issuer: str = Field(min_length=1)
    provenance: str = Field(min_length=1)
    expires_at: AwareDatetime
    state: Literal["active", "revoked", "terminal"] = "active"
    append_qualification: str | None = Field(default=None, min_length=1)
    create_qualification: str | None = Field(default=None, min_length=1)
    update_qualification: str | None = Field(default=None, min_length=1)

    def current(self) -> bool:
        return self.state == "active" and self.expires_at > datetime.now(UTC)

    def can_read(self, work_id: UUID, *, explicit_target: bool) -> bool:
        if self.scope == "workspace":
            return explicit_target
        return self.authority.can_read(work_id)

    def can_write(self, work_id: UUID) -> bool:
        if self.scope == "workspace":
            return True
        return work_id == self.authority.active_work_id


class ProtectedAppend(ClosedModel):
    api_version: Literal["1"]
    operation_id: UUID
    work_id: UUID
    grant_version: int = Field(ge=1)
    observed_revision: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=8000)


class ProtectedCreate(ClosedModel):
    api_version: Literal["1"]
    operation_id: UUID
    parent_work_id: UUID
    grant_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=500)
    notes: str = Field(default="", max_length=8000)


class ScalarPatch(ClosedModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    notes: str | None = Field(default=None, max_length=8000)
    completed: bool | None = None
    priority: str | None = Field(default=None, min_length=1)
    work_type: str | None = Field(default=None, min_length=1)
    review_next_action: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("patch must not be empty")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("patch values must not be null")
        if "review_next_action" in self.model_fields_set and "notes" not in self.model_fields_set:
            raise ValueError("review_next_action requires notes")
        return self


class ProtectedUpdate(ClosedModel):
    api_version: Literal["1"]
    operation_id: UUID
    work_id: UUID
    grant_version: int = Field(ge=1)
    observed_revision: str = Field(min_length=1)
    patch: ScalarPatch


class EffectReceipt(ClosedModel):
    operation_id: UUID
    principal: PrincipalContext
    grant_id: UUID
    grant_version: int
    work_id: UUID
    provider: str
    task_gid: str
    story_gid: str
    text: str
    qualification: str


class CreateReceipt(ClosedModel):
    operation_id: UUID
    principal: PrincipalContext
    grant_id: UUID
    grant_version: int
    work_id: UUID
    provider: str
    task_gid: str
    parent_task_gid: str
    title: str
    qualification: str


class UpdateReceipt(ClosedModel):
    operation_id: UUID
    principal: PrincipalContext
    grant_id: UUID
    grant_version: int
    work_id: UUID
    provider: str
    task_gid: str
    observed_revision: str
    resulting_revision: str
    patch: ScalarPatch
    qualification: str


class GuardOutcome(ClosedModel):
    status: Literal["ok", "denied", "stale", "not_applied", "unknown"]
    operation: str
    work_id: UUID | None = None
    operation_id: UUID | None = None
    reason: str
    effect: Literal["not_sent", "applied", "unknown"] = "not_sent"
    retry: Literal["none", "refresh", "reconcile"] = "none"
    next_action: str
    receipt: EffectReceipt | CreateReceipt | UpdateReceipt | None = None

    @model_validator(mode="after")
    def exact_receipt(self) -> Self:
        if self.effect == "applied" and (self.status != "ok" or self.receipt is None):
            raise ValueError("applied outcome requires an exact receipt")
        if self.receipt is not None and (self.effect != "applied" or self.status != "ok"):
            raise ValueError("only a successful applied outcome can claim a receipt")
        return self


class GrantResult(ClosedModel):
    status: Literal["ok", "denied", "unknown"]
    principal: PrincipalContext | None = None
    grant: WorkGrant | None = None
    guard: GuardOutcome | None = None


class GrantedWorkResult(ClosedModel):
    status: Literal["ok", "denied", "unknown", "provider_error", "stale"]
    item: WorkItem | None = None
    related: RelatedLookup | None = None
    guard: GuardOutcome | None = None
