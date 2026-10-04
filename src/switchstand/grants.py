"""Trusted caller/grant values. None of the issuance inputs are MCP arguments."""

import hashlib
from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from .contracts import ClosedModel, LaunchAuthority, WorkItem


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
        "work_get", "work_search", "work_append", "work_create", "work_update",
        "work_relate", "priority_claim", "message", "agent_task"
    ]]
    issuer: str = Field(min_length=1)
    provenance: str = Field(min_length=1)
    expires_at: AwareDatetime
    state: Literal["active", "revoked", "terminal"] = "active"
    append_qualification: str | None = Field(default=None, min_length=1)
    create_qualification: str | None = Field(default=None, min_length=1)
    update_qualification: str | None = Field(default=None, min_length=1)
    relation_qualification: str | None = Field(default=None, min_length=1)
    priority_claim_qualification: str | None = Field(default=None, min_length=1)

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
    grant_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=500)
    notes: str = Field(default="", max_length=8000)
    priority: str = Field(default="UNSET", min_length=1)
    work_type: str = Field(default="UNKNOWN", min_length=1)
    lifecycle_state: Literal[
        "CURRENT", "WAITING", "DEFERRED", "TERMINAL", "UNKNOWN"
    ] = "UNKNOWN"
    canonical_root: str | None = Field(default=None, min_length=1)
    owner_key: str = Field(default="UNKNOWN", min_length=1)
    wait_kind: str = Field(default="UNKNOWN", min_length=1)
    unblock_condition: str = Field(default="UNKNOWN", min_length=1)
    next_due: str = Field(default="UNKNOWN", min_length=1)
    next_action_class: str = Field(default="UNKNOWN", min_length=1)
    next_action_ref: str = Field(default="UNKNOWN", min_length=1)
    parent_work_id: UUID | None = None
    project_id: UUID | None = None
    project_gid: str | None = Field(default=None, pattern=r"^[0-9]+$")

    @model_validator(mode="after")
    def one_target(self) -> Self:
        if sum(value is not None for value in (
            self.parent_work_id, self.project_id, self.project_gid,
        )) != 1:
            raise ValueError("create requires exactly one parent or project target")
        return self


class ScalarPatch(ClosedModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    notes: str | None = Field(default=None, max_length=8000)
    completed: bool | None = None
    priority: str | None = Field(default=None, min_length=1)
    work_type: str | None = Field(default=None, min_length=1)
    review_next_action: str | None = Field(default=None, min_length=1)
    lifecycle_state: Literal[
        "CURRENT", "WAITING", "DEFERRED", "TERMINAL", "UNKNOWN"
    ] | None = None
    canonical_root: str | None = Field(default=None, min_length=1)
    owner_key: str | None = Field(default=None, min_length=1)
    wait_kind: str | None = Field(default=None, min_length=1)
    unblock_condition: str | None = Field(default=None, min_length=1)
    next_due: str | None = Field(default=None, min_length=1)
    next_action_class: str | None = Field(default=None, min_length=1)
    next_action_ref: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("patch must not be empty")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("patch values must not be null")
        if self.canonical_root not in {None, "NONE", "UNKNOWN"}:
            try:
                UUID(self.canonical_root)
            except ValueError as error:
                raise ValueError(
                    "canonical root must be NONE, UNKNOWN, or a WorkId"
                ) from error
        return self


class ProtectedUpdate(ClosedModel):
    api_version: Literal["1"]
    operation_id: UUID
    work_id: UUID
    grant_version: int = Field(ge=1)
    observed_revision: str = Field(min_length=1)
    patch: ScalarPatch


class RelationPatch(ClosedModel):
    kind: Literal["assignee", "placement", "parent", "dependency"]
    action: Literal["set", "clear", "add", "remove", "move"]
    target_work_id: UUID | None = None
    assignee_gid: str | None = Field(default=None, pattern=r"^[0-9]+$")
    project_gid: str | None = Field(default=None, pattern=r"^[0-9]+$")
    section_gid: str | None = Field(default=None, pattern=r"^[0-9]+$")

    @model_validator(mode="after")
    def valid_relation(self) -> Self:
        if self.kind == "assignee":
            if self.action not in {"set", "clear"}:
                raise ValueError("assignee relation requires set or clear")
            if (self.action == "set") != (self.assignee_gid is not None):
                raise ValueError("assignee target does not match action")
            if any(value is not None for value in (
                self.target_work_id, self.project_gid, self.section_gid
            )):
                raise ValueError("assignee relation forbids unrelated fields")
        elif self.kind == "placement":
            if (
                self.action not in {"add", "move", "remove"} or self.project_gid is None
            ):
                raise ValueError("provider placement requires project and add/move/remove")
            if self.target_work_id is not None or self.assignee_gid is not None or (
                self.action == "remove" and self.section_gid is not None
            ):
                raise ValueError("placement relation fields do not match action")
        elif self.kind == "parent":
            if self.action not in {"set", "clear"}:
                raise ValueError("parent relation requires set or clear")
            if (self.action == "set") != (self.target_work_id is not None):
                raise ValueError("parent target does not match action")
            if any(value is not None for value in (
                self.assignee_gid, self.project_gid, self.section_gid
            )):
                raise ValueError("parent relation forbids unrelated fields")
        else:
            if self.action not in {"add", "remove"} or self.target_work_id is None:
                raise ValueError("dependency relation requires target and add/remove")
            if any(value is not None for value in (
                self.assignee_gid, self.project_gid, self.section_gid
            )):
                raise ValueError("dependency relation forbids unrelated fields")
        return self


class ProtectedRelation(ClosedModel):
    api_version: Literal["1"]
    operation_id: UUID
    work_id: UUID
    grant_version: int = Field(ge=1)
    observed_revision: str = Field(min_length=1)
    patch: RelationPatch


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
    parent_task_gid: str | None = None
    project_gid: str | None = None
    title: str
    qualification: str


class RelationReceipt(ClosedModel):
    operation_id: UUID
    principal: PrincipalContext
    grant_id: UUID
    grant_version: int
    work_id: UUID
    provider: str
    task_gid: str
    observed_revision: str
    patch: RelationPatch
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


class PriorityClaimReceipt(ClosedModel):
    operation_id: UUID
    principal: PrincipalContext
    grant_id: UUID
    grant_version: int
    work_id: UUID
    claim_id: UUID
    supersedes_claim_id: UUID | None = None
    source_observed_revision: str
    qualification: str


class EffectOutcomeView(ClosedModel):
    """Sanitized effect truth without provider or principal internals."""
    status: Literal["ok", "denied", "stale", "not_applied", "unknown"]
    reason: str
    effect: Literal["not_sent", "applied", "unknown"]
    retry: Literal["none", "refresh", "reconcile"]


class EffectBlocker(ClosedModel):
    """Identity of an older unresolved effect blocking a new, unsent request."""
    operation: str
    operation_id: UUID
    work_id: UUID
    outcome: EffectOutcomeView


class GuardOutcome(ClosedModel):
    status: Literal["ok", "denied", "stale", "not_applied", "unknown"]
    operation: str
    work_id: UUID | None = None
    operation_id: UUID | None = None
    reason: str
    effect: Literal["not_sent", "applied", "unknown"] = "not_sent"
    retry: Literal["none", "refresh", "reconcile"] = "none"
    next_action: str
    receipt: (
        EffectReceipt | CreateReceipt | RelationReceipt | UpdateReceipt
        | PriorityClaimReceipt | None
    ) = None
    blocked_by: EffectBlocker | None = None

    @model_validator(mode="after")
    def exact_receipt(self) -> Self:
        if self.effect == "applied" and (self.status != "ok" or self.receipt is None):
            raise ValueError("applied outcome requires an exact receipt")
        if self.receipt is not None and (self.effect != "applied" or self.status != "ok"):
            raise ValueError("only a successful applied outcome can claim a receipt")
        if self.blocked_by is not None and (
            self.status != "unknown" or self.effect != "not_sent" or self.retry != "none"
        ):
            raise ValueError("a blocked request must remain explicitly unsent")
        return self


class GrantResult(ClosedModel):
    status: Literal["ok", "denied", "unknown"]
    principal: PrincipalContext | None = None
    grant: WorkGrant | None = None
    guard: GuardOutcome | None = None


class GrantedWorkResult(ClosedModel):
    status: Literal["ok", "denied", "unknown", "provider_error", "stale"]
    item: WorkItem | None = None
    guard: GuardOutcome | None = None
