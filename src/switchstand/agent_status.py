"""Latest self-reported cross-project work picture on an existing named mailbox."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from .agent_mailboxes import (
    CODEX_COORDINATOR_KEY,
    CODEX_COORDINATOR_NAME,
    AgentMailbox,
    agent_mailboxes,
)
from .contracts import ClosedModel


class StatusWrite(ClosedModel):
    operation_id: UUID
    expected_generation: int = Field(ge=0)
    report: str | None = Field(default=None, min_length=1, max_length=6000)
    coverage: Literal["OWNER_SEARCH_EXHAUSTED", "PARTIAL", "UNKNOWN"] | None = None
    defer_reason: str | None = Field(default=None, min_length=1, max_length=1000)
    recheck: str | None = Field(default=None, min_length=1, max_length=1000)
    defer_seconds: int | None = Field(default=None, ge=1, le=900)

    @model_validator(mode="after")
    def mode(self):
        report = self.report is not None and self.coverage is not None
        defer = all(x is not None for x in (self.defer_reason, self.recheck, self.defer_seconds))
        if report == defer or (report and any(x is not None for x in (
            self.defer_reason, self.recheck, self.defer_seconds,
        ))) or (defer and (self.report is not None or self.coverage is not None)):
            raise ValueError("Supply report+coverage OR concrete defer_reason+recheck+defer_seconds")
        if any(x is not None and not x.strip() for x in (self.report, self.defer_reason, self.recheck)):
            raise ValueError("Status text must not be blank")
        return self


class StatusResult(ClosedModel):
    status: str
    name: str | None = None
    owner_key: str | None = None
    generation: int = 0
    source: Literal["SELF_REPORTED"] = "SELF_REPORTED"
    binding_generation: int | None = None
    age_seconds: float | None = None
    due: bool = True
    document: dict[str, Any] | None = None
    next_action: str = "Reconcile current status before retrying; this report grants no authority."


def project(mailbox: AgentMailbox, document: dict[str, Any] | None) -> StatusResult:
    now = datetime.now(UTC)
    value = {k: v for k, v in (document or {}).items() if k != "receipt"}
    report = value.get("report")
    age = (now - datetime.fromisoformat(value["reported_at"])).total_seconds() if report else None
    stale = report and value["report_binding"] != mailbox.generation
    deferred = value.get("defer_binding") == mailbox.generation and (
        datetime.fromisoformat(value["defer_until"]) > now
    )
    owner = CODEX_COORDINATOR_NAME if mailbox.name_key == CODEX_COORDINATOR_KEY else f"agent:{mailbox.name_key}"
    return StatusResult(
        status="MISSING" if not report else "STALE_BINDING" if stale else "ok",
        name=mailbox.name, owner_key=owner, generation=value.get("generation", 0),
        binding_generation=mailbox.generation, age_seconds=age, document=value or None,
        due=not deferred and (age is None or bool(stale) or age >= 900),
        next_action=f"Read all pages of work_search(owner_key='{owner}', completed=False); reconcile actual assignments and known obligations. Coverage is self-attested, not complete remit. Then report finished/current/waiting-and-why/next, priority/scope and forgotten requests, or defer concretely. Continue authorized runnable work; preserve review watches.",
    )


class AgentStatusState:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def get(self, caller: AgentMailbox, target: AgentMailbox) -> StatusResult:
        async with self.engine.begin() as connection:
            rows = (await connection.execute(select(agent_mailboxes).where(
                agent_mailboxes.c.name_key.in_({caller.name_key, target.name_key}),
            ).with_for_update())).mappings().all()
            by_name = {row["name_key"]: row for row in rows}
            for identity in (caller, target):
                row = by_name.get(identity.name_key)
                if row is None or (row["principal_key"], row["session_key"], row["generation"]) != (
                    identity.principal_key, identity.session_key, identity.generation,
                ):
                    return StatusResult(status="UNKNOWN")
            return project(target, cast(dict[str, Any] | None, by_name[target.name_key]["agent_status"]))

    async def write(self, caller: AgentMailbox, request: StatusWrite) -> StatusResult:
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        async with self.engine.begin() as connection:
            row = (await connection.execute(select(agent_mailboxes).where(
                agent_mailboxes.c.name_key == caller.name_key,
            ).with_for_update())).mappings().one_or_none()
            if row is None or (row["principal_key"], row["session_key"], row["generation"]) != (
                caller.principal_key, caller.session_key, caller.generation,
            ):
                return StatusResult(status="STALE")
            old = cast(dict[str, Any], row["agent_status"] or {})
            if old.get("operation_id") == str(request.operation_id):
                return StatusResult.model_validate(old["receipt"]) if old["digest"] == digest else StatusResult(status="CONFLICT")
            if old.get("generation", 0) != request.expected_generation:
                return StatusResult(status="STALE")
            value = {k: v for k, v in old.items() if k not in {"receipt", "defer_until", "defer_binding", "defer_reason", "recheck"}}
            now = datetime.now(UTC)
            value.update(generation=request.expected_generation + 1, operation_id=str(request.operation_id), digest=digest)
            if request.report is not None:
                value.update(report=request.report, coverage=request.coverage, reported_at=now.isoformat(), report_binding=caller.generation)
            else:
                value.update(defer_reason=request.defer_reason, recheck=request.recheck, defer_binding=caller.generation,
                             defer_until=(now + timedelta(seconds=request.defer_seconds or 0)).isoformat())
            receipt = project(caller, value)
            stored = {**value, "receipt": receipt.model_dump(mode="json")}
            await connection.execute(update(agent_mailboxes).where(
                agent_mailboxes.c.name_key == caller.name_key,
            ).values(agent_status=stored))
            return receipt
