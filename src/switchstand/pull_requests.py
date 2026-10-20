"""PR identity and accepted ownership, inside the existing relation transaction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
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
    update,
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
    recipient_key: str | None = Field(default=None, min_length=1, max_length=80)
    acceptance_delivery_id: UUID | None = None
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


async def change(
    connection: AsyncConnection, work_id: UUID, action: str, patch: PullRequestChange,
    caller: AgentMailbox, principal_key: str, operation_id: UUID,
) -> PullRequestRecord:
    """Caller is server-resolved; fence it again under the database transaction."""
    from .messages import message_deliveries, messages
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
    ).with_for_update())).mappings().one()
    if patch.expected_version is not None and row["row_version"] != patch.expected_version:
        raise ValueError("PR version changed")
    if action == "set":
        if row["owner_key"] != caller.name_key or patch.expected_version is None:
            raise ValueError("only current PR owner can transfer exact version")
        recipient = (await connection.execute(select(agent_mailboxes).where(
            agent_mailboxes.c.name_key == patch.recipient_key
        ).with_for_update())).mappings().one()
        acceptance = await connection.scalar(select(messages.c.payload).join(
            message_deliveries,
            (messages.c.sender_work_id == message_deliveries.c.sender_work_id)
            & (messages.c.message_id == message_deliveries.c.message_id),
        ).where(
            message_deliveries.c.delivery_id == patch.acceptance_delivery_id,
            messages.c.sender_work_id == recipient["endpoint_id"],
            message_deliveries.c.recipient_work_id == caller.endpoint_id,
        ))
        if acceptance != {
            "kind": "pr_owner_acceptance", "repository": patch.repository,
            "number": patch.number, "previous_owner": caller.name_key,
            "expected_version": patch.expected_version, "package_ref": patch.package_ref,
        }:
            raise ValueError("exact durable recipient acceptance required")
        await connection.execute(update(pull_requests).where(key).values(
            owner_key=recipient["name_key"], row_version=row["row_version"] + 1,
            acceptance_ref=f"delivery:{patch.acceptance_delivery_id}",
        ))
    elif action == "add":
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
    records: tuple[PullRequestView, ...] = ()
    next_cursor: str | None = None
    has_more: bool = False


class PullRequestView(PullRequestRecord):
    work_ids: tuple[UUID, ...] = ()
    visibility: Literal["FULL", "PARTIAL"] = "FULL"
    link_has_more: bool = False
    link_next_cursor: str | None = None


class PageCursor(ClosedModel):
    basis: str
    offset: int = Field(ge=0)
    pr_key: str | None = None


async def project(
    connection: AsyncConnection, work_id: UUID, principal_key: str, revision: str,
    can_read: Callable[[UUID], Awaitable[bool]],
    pr_cursor: str | None, pr_link_cursor: str | None,
) -> PullRequestPage:
    """Page navigation, binding both cursors to current rows and readable backlinks."""
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
    views: list[PullRequestView] = []
    for row in rows:
        links = (await connection.scalars(select(work_pull_requests.c.work_id).where(
            work_pull_requests.c.repository == row["repository"],
            work_pull_requests.c.number == row["number"],
        ).order_by(work_pull_requests.c.work_id))).all()
        visible = tuple(link for link in links if await can_read(link))
        views.append(PullRequestView(
            **dict(row), work_ids=visible,
            visibility="FULL" if len(visible) == len(links) else "PARTIAL",
        ))
    basis = hashlib.sha256(json.dumps([
        principal_key, str(work_id), source,
        [view.model_dump(mode="json") for view in views],
    ], sort_keys=True).encode()).hexdigest()
    cursor = PageCursor.model_validate_json(pr_cursor or pr_link_cursor) if (
        pr_cursor or pr_link_cursor
    ) else PageCursor(basis=basis, offset=0)
    if (cursor.basis != basis or (pr_cursor and pr_link_cursor)
            or (pr_cursor and cursor.pr_key is not None)
            or (pr_link_cursor and cursor.pr_key is None)):
        return PullRequestPage(status="stale")
    selected = views[cursor.offset:cursor.offset + 10] if pr_link_cursor is None else [
        view for view in views if f"{view.repository}#{view.number}" == cursor.pr_key
    ]
    if pr_link_cursor and not selected:
        return PullRequestPage(status="stale")
    result: list[PullRequestView] = []
    for view in selected:
        offset = cursor.offset if pr_link_cursor else 0
        more = len(view.work_ids) > offset + 10
        result.append(view.model_copy(update={
            "work_ids": view.work_ids[offset:offset + 10], "link_has_more": more,
            "link_next_cursor": PageCursor(
                basis=basis, offset=offset + 10, pr_key=f"{view.repository}#{view.number}",
            ).model_dump_json() if more else None,
        }))
    more = not pr_link_cursor and len(views) > cursor.offset + 10
    return PullRequestPage(
        records=tuple(result), has_more=bool(more),
        next_cursor=PageCursor(basis=basis, offset=cursor.offset + 10).model_dump_json()
        if more else None,
    )
