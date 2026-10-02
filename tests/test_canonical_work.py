# pyright: reportPrivateUsage=false
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import DateTime, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncConnection

from switchstand.canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    _lock_and_bump_work_row,
)
from switchstand.state import canonical_work, work_events


def test_compact_tables_enforce_identity_and_version_contracts():
    assert canonical_work.primary_key.columns.keys() == ["work_id"]
    assert canonical_work.c.notes.nullable is False
    assert canonical_work.c.row_version.nullable is False
    assert next(iter(work_events.c.work_id.foreign_keys)).target_fullname == "canonical_work.work_id"
    assert isinstance(work_events.c.created_at.type, DateTime)
    assert work_events.c.created_at.type.timezone is True
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in work_events.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("work_id", "sequence") in unique_columns
    assert {index.name for index in work_events.indexes} >= {
        "ix_work_events_page", "uq_work_events_story", "uq_work_events_operation",
    }


class _Connection:
    def __init__(self, version: int | None):
        self.version = version
        self.executed: list[tuple[object, tuple[object, ...]]] = []

    async def scalar(self, statement: object) -> int | None:
        return self.version

    async def execute(self, statement: object, *args: object) -> None:
        self.executed.append((statement, args))


@pytest.mark.asyncio
async def test_lock_and_bump_is_caller_transaction_local():
    connection = _Connection(7)
    assert await _lock_and_bump_work_row(cast(AsyncConnection, connection), uuid4(), 7) == 8
    assert len(connection.executed) == 1

    with pytest.raises(ValueError, match="stale"):
        await _lock_and_bump_work_row(cast(AsyncConnection, _Connection(8)), uuid4(), 7)
    with pytest.raises(LookupError, match="does not exist"):
        await _lock_and_bump_work_row(cast(AsyncConnection, _Connection(None)), uuid4())


@pytest.mark.asyncio
async def test_relation_validation_rejects_self_and_duplicate_edges_before_writes():
    work_id, dependency = uuid4(), uuid4()
    with pytest.raises(ValueError, match="relate to itself"):
        await CanonicalWorkRepository._replace_relations(
            cast(AsyncConnection, object()),
            CurrentWork(work_id, "Title", False, "", parent_id=work_id),
        )
    with pytest.raises(ValueError, match="unique"):
        await CanonicalWorkRepository._replace_relations(
            cast(AsyncConnection, object()), CurrentWork(
                work_id, "Title", False, "", dependencies=(dependency, dependency)
            )
        )
