"""PR identity and accepted ownership, inside the existing relation transaction."""

from __future__ import annotations

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Table,
    Text,
    delete,
    select,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from .agent_mailboxes import AgentMailbox, agent_mailboxes
from .canonical_work import canonical_metadata, canonical_revision, canonical_work
from .contracts import ClosedModel

pull_requests = Table(
    "pull_requests", canonical_metadata,
    Column("repository", Text, primary_key=True),
    Column("number", BigInteger, primary_key=True),
    Column("owner_key", Text, nullable=False),
    Column("row_version", BigInteger, nullable=False),
    Column("acceptance_ref", Text, nullable=False),
    CheckConstraint("number > 0 AND row_version > 0", name="ck_pr_positive"),
)
work_pull_requests = Table(
    "work_pull_requests", canonical_metadata,
    Column("work_id", PGUUID(as_uuid=True),
           ForeignKey(canonical_work.c.work_id, ondelete="RESTRICT"), primary_key=True),
    Column("repository", Text, primary_key=True),
    Column("number", BigInteger, primary_key=True),
    ForeignKeyConstraint(["repository", "number"],
                         [pull_requests.c.repository, pull_requests.c.number],
                         ondelete="RESTRICT"),
)


class PullRequestChange(ClosedModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)
    number: int = Field(gt=0)
    expected_version: int | None = Field(default=None, gt=0)
    package_ref: str = Field(min_length=1, max_length=200)

    @field_validator("repository")
    @classmethod
    def normalized(cls, value: str) -> str:
        return value.lower()


class PullRequestRecord(ClosedModel):
    repository: str
    number: int
    owner_key: str
    row_version: int
    acceptance_ref: str


class StaleVersion(ValueError):
    """An expected PR revision is no longer current; no mutation was sent."""


async def change(
    connection: AsyncConnection, work_id: UUID, action: str, patch: PullRequestChange,
    caller: AgentMailbox, principal_key: str, operation_id: UUID,
) -> PullRequestRecord:
    """Caller is server-resolved; fence it again under the database transaction."""
    binding = (await connection.execute(select(agent_mailboxes).where(
        agent_mailboxes.c.name_key == caller.name_key
    ).with_for_update())).mappings().one_or_none()
    if binding is None or any(binding[key] != getattr(caller, key) for key in (
        "endpoint_id", "principal_key", "session_key", "generation",
    )) or caller.principal_key != principal_key:
        raise ValueError("registered caller changed")
    key = (pull_requests.c.repository == patch.repository) & (
        pull_requests.c.number == patch.number
    )
    if action == "add":
        await connection.execute(insert(pull_requests).values(
            repository=patch.repository, number=patch.number, owner_key=caller.name_key,
            row_version=1, acceptance_ref=f"operation:{operation_id}",
        ).on_conflict_do_nothing())
    row = (await connection.execute(select(pull_requests).where(
        key
    ).with_for_update())).mappings().one_or_none()
    if row is None:
        raise ValueError("PR identity does not exist")
    link = (work_pull_requests.c.work_id == work_id) & (
        work_pull_requests.c.repository == patch.repository
    ) & (work_pull_requests.c.number == patch.number)
    if action == "remove" and await connection.scalar(
        select(work_pull_requests.c.work_id).where(link)
    ) is None:
        raise ValueError("PR is not linked to this work")
    if patch.expected_version is not None and row["row_version"] != patch.expected_version:
        raise StaleVersion("PR version changed")
    if action == "add":
        await connection.execute(insert(work_pull_requests).values(
            work_id=work_id, repository=patch.repository, number=patch.number,
        ).on_conflict_do_nothing())
    elif action == "remove":
        await connection.execute(delete(work_pull_requests).where(
            work_pull_requests.c.work_id == work_id,
            work_pull_requests.c.repository == patch.repository,
            work_pull_requests.c.number == patch.number,
        ))
    else:
        raise ValueError("unsupported PR action")
    result = (await connection.execute(select(pull_requests).where(key))).mappings().one()
    return PullRequestRecord.model_validate(dict(result))


class PullRequestPage(ClosedModel):
    status: Literal["ok", "stale"] = "ok"
    records: tuple[PullRequestRecord, ...] = ()
    next_cursor: str | None = None
    has_more: bool = False


class PageCursor(ClosedModel):
    basis: str
    offset: int = Field(ge=0)


async def project(
    connection: AsyncConnection, work_id: UUID, principal_key: str, revision: str,
    pr_cursor: str | None,
) -> PullRequestPage:
    """Page task-linked PRs, binding continuation to the caller and current rows."""
    source = await connection.scalar(select(canonical_work.c.row_version).where(
        canonical_work.c.work_id == work_id
    ))
    if source is None or canonical_revision(work_id, source) != revision:
        return PullRequestPage(status="stale")
    rows = (await connection.execute(select(pull_requests).join(
        work_pull_requests,
        (pull_requests.c.repository == work_pull_requests.c.repository)
        & (pull_requests.c.number == work_pull_requests.c.number),
    ).where(work_pull_requests.c.work_id == work_id).order_by(
        pull_requests.c.repository, pull_requests.c.number,
    ))).mappings().all()
    records = [PullRequestRecord.model_validate(dict(row)) for row in rows]
    basis = hashlib.sha256(json.dumps([
        principal_key, str(work_id), source,
        [record.model_dump(mode="json") for record in records],
    ], sort_keys=True).encode()).hexdigest()
    cursor = PageCursor.model_validate_json(pr_cursor) if pr_cursor else PageCursor(
        basis=basis, offset=0,
    )
    if cursor.basis != basis:
        return PullRequestPage(status="stale")
    more = len(records) > cursor.offset + 10
    return PullRequestPage(
        records=tuple(records[cursor.offset:cursor.offset + 10]), has_more=more,
        next_cursor=PageCursor(basis=basis, offset=cursor.offset + 10).model_dump_json()
        if more else None,
    )
