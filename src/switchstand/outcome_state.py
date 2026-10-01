"""Inert owner-local outcome snapshots and deterministic action projection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Self, cast
from uuid import UUID, uuid4

from pydantic import Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from .contracts import ClosedModel
from .state import outcome_state_revisions, work_handles


class OutcomeItem(ClosedModel):
    item_key: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    kind: Literal["DELIVERABLE", "DECISION"]
    status: Literal["NOT_STARTED", "READY", "IN_PROGRESS", "DONE"]
    who_acts: Literal["OWNER", "MARCO"]
    dispatch_work_id: UUID | None = None
    what_yes_causes: str | None = Field(default=None, min_length=1, max_length=1000)
    source_label: Literal["AGENT", "MARCO"] = "AGENT"
    source_ref: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def valid_action_shape(self) -> Self:
        dispatch = (
            self.kind == "DELIVERABLE" and self.status == "READY"
            and self.who_acts == "MARCO"
        )
        decision = (
            self.kind == "DECISION" and self.status in {"NOT_STARTED", "READY"}
            and self.who_acts == "MARCO"
        )
        if dispatch != (self.dispatch_work_id is not None):
            raise ValueError("dispatch identity must match a ready Marco deliverable")
        if decision != (self.what_yes_causes is not None):
            raise ValueError("decision consequence must match an actionable Marco decision")
        if self.who_acts == "MARCO" and self.kind == "DELIVERABLE" \
                and self.status == "NOT_STARTED":
            raise ValueError("Marco deliverable must be ready, in progress, or done")
        return self


class OutcomeAction(ClosedModel):
    action_class: Literal["NEEDS_MARCO", "READY_TO_DISPATCH", "OWNER_CAN_DO"]
    item_key: str
    description: str
    dispatch_work_id: UUID | None = None
    what_yes_causes: str | None = None


class ActionSummary(ClosedModel):
    currentness: Literal["CURRENT", "STALE"]
    open_action_count: int = Field(ge=0)
    actions: tuple[OutcomeAction, ...]


@dataclass(frozen=True)
class OutcomeWrite:
    status: Literal["APPLIED", "REPLAYED", "STALE", "CONFLICT", "DENIED", "UNKNOWN"]
    state_id: UUID | None = None


@dataclass(frozen=True)
class _Revision:
    state_id: UUID
    operation_id: UUID
    owner_work_id: UUID
    generation: int
    predecessor_id: UUID | None
    items: tuple[OutcomeItem, ...]
    owner_currentness_token: str
    content_digest: str


_COLUMNS = tuple(outcome_state_revisions.c)


def _canonical(items: tuple[OutcomeItem, ...]) -> list[dict[str, object]]:
    if len(items) > 64 or len({item.item_key for item in items}) != len(items):
        raise ValueError("outcome items must be unique and bounded")
    return [item.model_dump(mode="json") for item in items]


def _digest(
    owner: UUID, predecessor: UUID | None, token: str, items: tuple[OutcomeItem, ...],
) -> str:
    value = [str(owner), None if predecessor is None else str(predecessor), token,
             _canonical(items)]
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


def _row(value: Mapping[Any, Any]) -> _Revision:
    return _Revision(
        cast(UUID, value["state_id"]), cast(UUID, value["operation_id"]),
        cast(UUID, value["owner_work_id"]), cast(int, value["generation"]),
        cast(UUID | None, value["predecessor_id"]), tuple(
            OutcomeItem.model_validate(item) for item in cast(list[object], value["items"])
        ), cast(str, value["owner_currentness_token"]), cast(str, value["content_digest"]),
    )


def _chain(values: Sequence[Mapping[Any, Any]]) -> tuple[_Revision, ...] | None:
    try:
        rows = tuple(_row(value) for value in values)
        predecessor = None
        for generation, row in enumerate(rows, 1):
            if (
                row.generation != generation or row.predecessor_id != predecessor
                or row.content_digest != _digest(
                    row.owner_work_id, row.predecessor_id,
                    row.owner_currentness_token, row.items,
                )
            ):
                return None
            predecessor = row.state_id
        return rows
    except (TypeError, ValueError, ValidationError):
        return None


def _summary(row: _Revision, token: str) -> ActionSummary:
    if row.owner_currentness_token != token:
        stale_actions = (OutcomeAction(
            action_class="OWNER_CAN_DO", item_key="OUTCOME_STATE_STALE",
            description="Reconcile the outcome snapshot against current owner work.",
        ),)
        return ActionSummary(currentness="STALE", open_action_count=1, actions=stale_actions)
    actions: list[OutcomeAction] = []
    for item in row.items:
        if item.status == "DONE" or (item.who_acts == "MARCO" and item.status == "IN_PROGRESS"):
            continue
        if item.dispatch_work_id is not None:
            action_class = "READY_TO_DISPATCH"
        elif item.who_acts == "MARCO":
            action_class = "NEEDS_MARCO"
        else:
            action_class = "OWNER_CAN_DO"
        actions.append(OutcomeAction(
            action_class=action_class, item_key=item.item_key, description=item.description,
            dispatch_work_id=item.dispatch_work_id, what_yes_causes=item.what_yes_causes,
        ))
    order = {"NEEDS_MARCO": 0, "READY_TO_DISPATCH": 1, "OWNER_CAN_DO": 2}
    actions.sort(key=lambda action: (order[action.action_class], action.item_key))
    return ActionSummary(currentness="CURRENT", open_action_count=len(actions), actions=tuple(actions))


class OutcomeStateStore:
    """Append/read owner snapshots; exact launch identity is the only V1 writer admission."""

    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def summary(
        self, owner_work_id: UUID, currentness_token: str,
    ) -> ActionSummary | Literal["UNKNOWN"] | None:
        async with self.engine.connect() as connection:
            values = (await connection.execute(select(*_COLUMNS).where(
                outcome_state_revisions.c.owner_work_id == owner_work_id
            ).order_by(outcome_state_revisions.c.generation))).mappings().all()
        chain = _chain(list(values))
        if chain == ():
            return None
        if chain is None:
            return "UNKNOWN"
        return _summary(chain[-1], currentness_token)

    async def record(
        self, *, owner_work_id: UUID, active_work_id: UUID | None, operation_id: UUID,
        expected_state_id: UUID | None, owner_currentness_token: str,
        items: tuple[OutcomeItem, ...],
    ) -> OutcomeWrite:
        if active_work_id != owner_work_id:
            return OutcomeWrite("DENIED")
        if not owner_currentness_token:
            raise ValueError("owner currentness token is required")
        if any(item.source_label == "MARCO" for item in items):
            return OutcomeWrite("DENIED")
        digest = _digest(owner_work_id, expected_state_id, owner_currentness_token, items)
        async with self.engine.begin() as connection:
            handle = (await connection.execute(select(work_handles.c.id).where(
                work_handles.c.id == owner_work_id
            ).with_for_update())).scalar_one_or_none()
            if handle is None:
                return OutcomeWrite("UNKNOWN")
            values = (await connection.execute(select(*_COLUMNS).where(
                outcome_state_revisions.c.owner_work_id == owner_work_id
            ).order_by(outcome_state_revisions.c.generation))).mappings().all()
            chain = _chain(list(values))
            if chain is None:
                return OutcomeWrite("UNKNOWN")
            replay = (await connection.execute(select(*_COLUMNS).where(
                outcome_state_revisions.c.operation_id == operation_id
            ))).mappings().one_or_none()
            if replay is not None:
                row = _row(replay)
                if row.content_digest != _digest(
                    row.owner_work_id, row.predecessor_id, row.owner_currentness_token, row.items,
                ):
                    return OutcomeWrite("UNKNOWN", row.state_id)
                if row.owner_work_id == owner_work_id:
                    return OutcomeWrite(
                        "REPLAYED" if row.content_digest == digest else "CONFLICT", row.state_id,
                    )
                return OutcomeWrite("CONFLICT", row.state_id)
            head = None if not chain else chain[-1]
            if expected_state_id != (None if head is None else head.state_id):
                return OutcomeWrite("STALE", None if head is None else head.state_id)
            state_id = uuid4()
            inserted = (await connection.execute(insert(outcome_state_revisions).values(
                state_id=state_id, operation_id=operation_id, owner_work_id=owner_work_id,
                generation=1 if head is None else head.generation + 1,
                predecessor_id=expected_state_id, schema_version=1, items=_canonical(items),
                owner_currentness_token=owner_currentness_token, content_digest=digest,
            ).on_conflict_do_nothing().returning(*_COLUMNS))).mappings().one_or_none()
            if inserted is None:
                collision = (await connection.execute(select(*_COLUMNS).where(
                    outcome_state_revisions.c.operation_id == operation_id
                ))).mappings().one_or_none()
                if collision is None:
                    return OutcomeWrite("UNKNOWN")
                row = _row(collision)
                if row.content_digest != _digest(
                    row.owner_work_id, row.predecessor_id, row.owner_currentness_token, row.items,
                ):
                    return OutcomeWrite("UNKNOWN", row.state_id)
                return OutcomeWrite("CONFLICT", row.state_id)
            if _row(inserted).content_digest != digest:
                raise ValueError("outcome state readback mismatch")
            return OutcomeWrite("APPLIED", state_id)
