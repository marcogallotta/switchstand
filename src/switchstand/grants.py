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
    operations: frozenset[Literal["work_get", "work_append"]]
    issuer: str = Field(min_length=1)
    provenance: str = Field(min_length=1)
    expires_at: AwareDatetime
    state: Literal["active", "revoked", "terminal"] = "active"
    append_qualification: str | None = Field(default=None, min_length=1)

    def current(self) -> bool:
        return self.state == "active" and self.expires_at > datetime.now(UTC)


class ProtectedAppend(ClosedModel):
    api_version: Literal["1"]
    operation_id: UUID
    work_id: UUID
    grant_version: int = Field(ge=1)
    observed_revision: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=8000)


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


class GuardOutcome(ClosedModel):
    status: Literal["ok", "denied", "stale", "not_applied", "unknown"]
    operation: str
    work_id: UUID | None = None
    operation_id: UUID | None = None
    reason: str
    effect: Literal["not_sent", "applied", "unknown"] = "not_sent"
    retry: Literal["none", "refresh", "reconcile"] = "none"
    next_action: str
    receipt: EffectReceipt | None = None

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
    guard: GuardOutcome | None = None
