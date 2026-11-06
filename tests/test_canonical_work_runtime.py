from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from switchstand.contracts import WorkSearchRequest
from switchstand.tracker.relations import ProjectPlacement, WorkRelations
from switchstand.tracker.runtime import CanonicalWorkRuntime
from switchstand.tracker.work import CurrentWork, CurrentWorkPage, canonical_revision


def _work(work_id: UUID, version: int = 1) -> CurrentWork:
    return CurrentWork(
        work_id, "Task", False, "notes", row_version=version, assignee="Marco",
        priority="P0", work_type="Implementation", lifecycle_state="CURRENT",
        wait_kind="NONE", unblock_condition="NONE", next_due="NONE",
    )


def _relations(area: str = "Switchstand", stage: str = "Doing") -> WorkRelations:
    return WorkRelations(
        None, (), (ProjectPlacement(uuid4(), area, "123", stage),),
    )


@pytest.fixture(autouse=True)
def no_approval_in_projection_unit_fakes(monkeypatch):
    monkeypatch.setattr(CanonicalWorkRuntime, "_task_approval", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_get_projects_only_postgres_state() -> None:
    work_id = uuid4()
    works = SimpleNamespace(get=AsyncMock(return_value=_work(work_id)))
    relations = SimpleNamespace(get=AsyncMock(return_value=_relations()))
    runtime = CanonicalWorkRuntime(works, relations)  # type: ignore[arg-type]

    result = await runtime.get(work_id)

    assert result.status == "ok"
    assert result.item is not None
    assert result.item.revision == canonical_revision(work_id, 1)
    assert result.item.source is None
    assert result.item.routing.priority == "P0"
    assert result.item.context.model_dump() == {
        "assignee": "Marco",
        "placements": ({"area": "Switchstand", "stage": "Doing"},),
    }


@pytest.mark.asyncio
async def test_search_projects_page_and_cursor() -> None:
    work_id = uuid4()
    work = _work(work_id)
    works = SimpleNamespace(
        get=AsyncMock(return_value=work),
        search=AsyncMock(return_value=CurrentWorkPage((work,), "next")),
    )
    relations = SimpleNamespace(get=AsyncMock(return_value=_relations()))
    runtime = CanonicalWorkRuntime(works, relations)  # type: ignore[arg-type]
    request = WorkSearchRequest(
        api_version="1", text="task", completed=False, cursor="cursor", limit=7,
        owner_key="agent:root", lifecycle_state="CURRENT",
    )

    result = await runtime.search(request)

    assert result.status == "ok"
    assert result.next_cursor == "next"
    assert [item.id for item in result.items] == [work_id]
    works.search.assert_awaited_once_with(
        "task", completed=False, cursor="cursor", limit=7,
        owner_key="agent:root", lifecycle_state="CURRENT",
    )


@pytest.mark.asyncio
async def test_get_retries_when_placements_cross_a_row_version() -> None:
    work_id = uuid4()
    before, after = _work(work_id), _work(work_id, 2)
    old_relations, new_relations = _relations("Mailbox", "Doing"), _relations("Tracker", "Review")
    works = SimpleNamespace(get=AsyncMock(side_effect=[before, after, after, after]))
    relations = SimpleNamespace(get=AsyncMock(side_effect=[old_relations, new_relations]))
    runtime = CanonicalWorkRuntime(works, relations)  # type: ignore[arg-type]

    result = await runtime.get(work_id)

    assert result.status == "ok"
    assert result.item is not None
    assert result.item.revision == canonical_revision(work_id, 2)
    assert result.item.context.model_dump()["placements"] == (
        {"area": "Tracker", "stage": "Review"},
    )


@pytest.mark.asyncio
async def test_search_retries_whole_page_when_placements_cross_a_row_version() -> None:
    first_id, second_id = uuid4(), uuid4()
    first_before, first_after = _work(first_id), _work(first_id, 2)
    second_before, second_after = _work(second_id), _work(second_id, 2)
    current = {
        first_id: iter((first_before, first_after)),
        second_id: iter((second_after, second_after)),
    }
    placements = {
        first_id: iter((_relations("Mailbox", "Doing"), _relations("Tracker", "Review"))),
        second_id: iter((_relations("Before", "Doing"), _relations("After", "Review"))),
    }
    works = SimpleNamespace(
        get=AsyncMock(side_effect=lambda work_id: next(current[work_id])),
        search=AsyncMock(side_effect=[
            CurrentWorkPage((first_before, second_before), "old-cursor"),
            CurrentWorkPage((first_after, second_after), "new-cursor"),
        ]),
    )
    relations = SimpleNamespace(get=AsyncMock(side_effect=lambda work_id: next(placements[work_id])))
    runtime = CanonicalWorkRuntime(works, relations)  # type: ignore[arg-type]

    result = await runtime.search(WorkSearchRequest(api_version="1", limit=7))

    assert result.status == "ok"
    assert result.next_cursor == "new-cursor"
    assert [
        (item.id, item.revision, item.context.model_dump()["placements"])
        for item in result.items
    ] == [
        (first_id, canonical_revision(first_id, 2), ({"area": "Tracker", "stage": "Review"},)),
        (second_id, canonical_revision(second_id, 2), ({"area": "After", "stage": "Review"},)),
    ]
    assert works.search.await_count == 2
