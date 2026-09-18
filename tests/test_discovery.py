from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from switchstand.contracts import Routing, WorkSearchRequest
from switchstand.core import Handle, ProviderError
from switchstand.discovery import (
    ProviderSearchItem,
    ProviderSearchPage,
    WorkDiscovery,
)
from switchstand.provider import PROJECT, PROJECTS, AsanaProvider


def task(
    gid: str, *, title: str | None = None, completed: bool = False,
    revision: str = "r1", project: str = PROJECT, priority: str = "P0",
) -> dict[str, object]:
    return {
        "gid": gid,
        "name": title or f"Task {gid}",
        "notes": "",
        "completed": completed,
        "modified_at": revision,
        "memberships": [{"project": {"gid": project}}],
        "parent": None,
        "custom_fields": [{
            "gid": "1217653169990249",
            "display_value": priority,
            "enabled": True,
            "resource_subtype": "enum",
            "enum_value": {"gid": "priority-option"},
            "enum_options": [],
        }],
    }


def asana_provider(*responses: dict) -> tuple[AsanaProvider, list[httpx.Request]]:
    pending = list(responses)
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if not pending:
            raise AssertionError(f"unexpected request {request.url}")
        return httpx.Response(200, request=request, json=pending.pop(0))

    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0",
        transport=httpx.MockTransport(answer),
    )
    return AsanaProvider(client), requests


async def test_asana_search_is_bounded_to_admitted_projects_and_exact_filters():
    subject, requests = asana_provider({
        "data": [task("101", title="Needle", priority="P1")],
        "next_page": {"offset": "next-page"},
    })
    page = await subject.search_work("needle", False, None, 25)
    assert page.next_cursor == "next-page"
    assert page.items == (
        ProviderSearchItem(
            provider_work_id="101", title="Needle", completed=False,
            revision="r1", routing=Routing(priority="P1"),
        ),
    )
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path.endswith("/workspaces/1200569426771227/tasks/search")
    assert request.url.params["text"] == "needle"
    assert request.url.params["completed"] == "false"
    assert request.url.params["limit"] == "25"
    assert set(request.url.params["projects.any"].split(",")) == set(PROJECTS)
    assert "offset" not in request.url.params


async def test_asana_search_uses_returned_cursor_and_allows_list_without_text():
    subject, requests = asana_provider({
        "data": [task("102", completed=True)],
        "next_page": None,
    })
    page = await subject.search_work(None, True, "cursor-2", 50)
    assert page.next_cursor is None and page.items[0].completed is True
    request = requests[0]
    assert request.url.params["offset"] == "cursor-2"
    assert request.url.params["completed"] == "true"
    assert "text" not in request.url.params


@pytest.mark.parametrize("problem", ["foreign", "duplicate", "bad_cursor", "too_many"])
async def test_asana_search_fails_closed_on_untrusted_or_invalid_page(problem):
    rows = [task("101")]
    next_page: object = None
    if problem == "foreign":
        rows = [task("101", project="999999")]
    elif problem == "duplicate":
        rows = [task("101"), task("101")]
    elif problem == "bad_cursor":
        next_page = {"offset": ""}
    else:
        rows = [task(str(index)) for index in range(3)]
    subject, _requests = asana_provider({"data": rows, "next_page": next_page})
    limit = 2 if problem == "too_many" else 50
    with pytest.raises(ProviderError):
        await subject.search_work(None, None, None, limit)


class MemoryState:
    def __init__(self):
        self.handles: dict[tuple[str, str], Handle] = {}

    async def bind(self, provider: str, provider_work_id: str) -> Handle:
        key = provider, provider_work_id
        if key not in self.handles:
            self.handles[key] = Handle(uuid4(), provider, provider_work_id)
        return self.handles[key]


class FakeProvider:
    def __init__(self):
        self.calls: list[tuple[object, ...]] = []

    async def search_work(self, text, completed, cursor, limit):
        self.calls.append((text, completed, cursor, limit))
        return ProviderSearchPage(
            items=(
                ProviderSearchItem(
                    provider_work_id="101", title="Alpha", completed=False,
                    revision="r1", routing=Routing(priority="P0"),
                ),
                ProviderSearchItem(
                    provider_work_id="202", title="Beta", completed=True,
                    revision="r2", routing=Routing(priority="P1"),
                ),
            ),
            next_cursor="next",
        )


async def test_discovery_returns_only_stable_provider_neutral_work_ids():
    provider = FakeProvider()
    state = MemoryState()
    subject = WorkDiscovery("asana", provider, state)
    request = WorkSearchRequest(
        api_version="1", text="task", completed=None, cursor=None, limit=10
    )
    first = await subject.search(request)
    second = await subject.search(request)
    assert first.status == second.status == "ok"
    assert first.next_cursor == second.next_cursor == "next"
    assert [item.id for item in first.items] == [item.id for item in second.items]
    assert [item.title for item in first.items] == ["Alpha", "Beta"]
    assert provider.calls == [("task", None, None, 10), ("task", None, None, 10)]
    rendered = first.model_dump(mode="json")
    assert all("provider" not in item and "task_gid" not in item for item in rendered["items"])


class BrokenProvider:
    async def search_work(self, *_args):
        raise ProviderError("hidden provider detail")


async def test_discovery_provider_failure_returns_closed_error_without_partial_data():
    result = await WorkDiscovery("asana", BrokenProvider(), MemoryState()).search(
        WorkSearchRequest(api_version="1")
    )
    assert result.status == "provider_error"
    assert result.items == () and result.next_cursor is None


@pytest.mark.parametrize("values", [
    {"text": ""},
    {"cursor": ""},
    {"limit": 0},
    {"limit": 101},
    {"unexpected": True},
])
def test_discovery_request_contract_is_closed_and_bounded(values):
    with pytest.raises(ValidationError):
        WorkSearchRequest.model_validate({"api_version": "1"} | values)
