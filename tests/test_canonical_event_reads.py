from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from switchstand.canonical_event_reads import CanonicalEventReader
from switchstand.canonical_work import CurrentWork, canonical_revision
from switchstand.contracts import WorkEventRequest, WorkHistoryRequest
from switchstand.work_events import StoredWorkEvent, WorkEventPage


def _work(work_id: UUID, version: int = 1) -> CurrentWork:
    return CurrentWork(work_id, "Task", False, "notes", row_version=version)


def _event(work_id: UUID) -> StoredWorkEvent:
    return StoredWorkEvent(
        id=uuid4(), work_id=work_id, sequence=1, result_version=1,
        subtype="comment", text="hello", created_at=datetime(2026, 10, 2, tzinfo=UTC),
        actor="Marco", asana_story_gid="123", operation_id=None,
    )


@pytest.mark.asyncio
async def test_history_projects_stable_db_events() -> None:
    work_id = uuid4()
    event = _event(work_id)
    works = SimpleNamespace(get=AsyncMock(side_effect=[_work(work_id), _work(work_id)]))
    events = SimpleNamespace(page=AsyncMock(return_value=WorkEventPage((event,), "next")))
    reader = CanonicalEventReader(works, events)  # type: ignore[arg-type]

    result = await reader.history(WorkHistoryRequest(
        api_version="1", work_id=work_id,
        observed_revision=canonical_revision(work_id, 1), limit=7,
    ))

    assert result.status == "ok"
    assert result.revision == canonical_revision(work_id, 1)
    assert result.next_cursor == "next"
    assert result.events[0].model_dump() == {
        "id": event.id, "work_id": work_id, "subtype": "comment", "text": "hello",
        "created_at": "2026-10-02T00:00:00+00:00", "actor": "Marco",
    }
    events.page.assert_awaited_once_with(work_id, cursor=None, limit=7)


@pytest.mark.asyncio
async def test_history_rejects_stale_revision_before_reading_events() -> None:
    work_id = uuid4()
    works = SimpleNamespace(get=AsyncMock(return_value=_work(work_id, 2)))
    events = SimpleNamespace(page=AsyncMock())
    reader = CanonicalEventReader(works, events)  # type: ignore[arg-type]

    result = await reader.history(WorkHistoryRequest(
        api_version="1", work_id=work_id,
        observed_revision=canonical_revision(work_id, 1),
    ))

    assert result.status == "stale"
    assert result.revision == canonical_revision(work_id, 2)
    events.page.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_does_not_claim_events_when_work_changes_during_read() -> None:
    work_id = uuid4()
    works = SimpleNamespace(get=AsyncMock(side_effect=[_work(work_id), _work(work_id, 2)]))
    events = SimpleNamespace(page=AsyncMock(return_value=WorkEventPage((_event(work_id),), None)))
    reader = CanonicalEventReader(works, events)  # type: ignore[arg-type]

    result = await reader.history(WorkHistoryRequest(
        api_version="1", work_id=work_id,
        observed_revision=canonical_revision(work_id, 1),
    ))

    assert result.status == "stale"
    assert result.revision == canonical_revision(work_id, 2)
    assert result.events == ()


@pytest.mark.asyncio
async def test_exact_event_is_work_scoped_and_unknown_when_absent() -> None:
    work_id, event_id = uuid4(), uuid4()
    work = _work(work_id)
    works = SimpleNamespace(get=AsyncMock(side_effect=[work, work]))
    events = SimpleNamespace(get=AsyncMock(return_value=None))
    reader = CanonicalEventReader(works, events)  # type: ignore[arg-type]

    result = await reader.event(WorkEventRequest(
        api_version="1", work_id=work_id, event_id=event_id,
        observed_revision=canonical_revision(work_id, 1),
    ))

    assert result.status == "unknown"
    assert result.item is None
    events.get.assert_awaited_once_with(work_id, event_id)
