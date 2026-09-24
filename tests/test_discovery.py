from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from switchstand.contracts import Routing, WorkContext, WorkSearchRequest
from switchstand.core import Handle, ProviderError
from switchstand.discovery import ProviderSearchItem, ProviderSearchPage, WorkDiscovery
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
        "assignee": None,
        "memberships": [{"project": {"gid": project, "name": f"Area {project}"},
                         "section": None}],
        "parent": None,
        "custom_fields": [{
            "gid": "1217653169990249",
            "display_value": priority,
            "enabled": True,
            "resource_subtype": "enum",
            "enum_value": {"gid": "priority-option"},
            "enum_options": [{"gid": "priority-option", "name": priority, "enabled": True}],
        }],
    }


def asana_provider(
    *responses: dict, test_project_gid: str | None = None,
) -> tuple[AsanaProvider, list[httpx.Request]]:
    pending = list(responses)
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if not pending:
            raise AssertionError(f"unexpected request {request.url}")
        return httpx.Response(200, request=request, json=pending.pop(0))

    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", transport=httpx.MockTransport(answer)
    )
    return AsanaProvider(
        client, test_project_gid, test_only=test_project_gid is not None
    ), requests


async def test_text_search_is_bounded_non_continuable_and_exact_read_back():
    subject, requests = asana_provider(
        {"data": [{"gid": "101"}], "next_page": None},
        {"data": task("101", title="Needle", priority="P1")},
    )
    page = await subject.search_work("needle", False, None, 25)
    assert page.next_cursor is None
    assert page.items == (
        ProviderSearchItem(
            provider_work_id="101", title="Needle", completed=False,
            revision="r1", routing=Routing(priority="P1"),
            context=WorkContext(placements=({"area": f"Area {PROJECT}"},)),
        ),
    )
    assert len(requests) == 2
    request = requests[0]
    assert request.url.path.endswith("/workspaces/1200569426771227/tasks/search")
    assert request.url.params["text"] == "needle"
    assert request.url.params["completed"] == "false"
    assert request.url.params["limit"] == "25"
    assert set(request.url.params["projects.any"].split(",")) == set(PROJECTS)
    assert "offset" not in request.url.params
    assert requests[1].url.path.endswith("/tasks/101")


async def test_text_search_rejects_uncontinuable_provider_page():
    subject, requests = asana_provider({
        "data": [{"gid": "101"}],
        "next_page": {"offset": "not-supported-here"},
    })
    with pytest.raises(ProviderError):
        await subject.search_work("needle", None, None, 25)
    assert len(requests) == 1


async def test_text_search_rejects_cursor_without_provider_request():
    subject, requests = asana_provider()
    with pytest.raises(ProviderError):
        await subject.search_work("needle", None, "invented-offset", 25)
    assert requests == []


async def test_list_uses_real_project_offsets_then_advances_admitted_projects():
    subject, requests = asana_provider(
        {"data": [{"gid": "101"}], "next_page": {"offset": "provider-offset"}},
        {"data": task("101")},
        {"data": [{"gid": "102"}], "next_page": None},
        {"data": task("102", completed=True)},
        {"data": [], "next_page": None},
    )
    first = await subject.search_work(None, None, None, 1)
    second = await subject.search_work(None, None, first.next_cursor, 1)
    third = await subject.search_work(None, None, second.next_cursor, 1)
    projects = sorted(PROJECTS)
    assert first.next_cursor == "0:provider-offset"
    assert second.next_cursor == "1:" and third.next_cursor == "2:"
    assert [item.provider_work_id for item in first.items + second.items] == ["101", "102"]
    assert requests[0].url.path.endswith("/tasks")
    assert requests[0].url.params["project"] == projects[0]
    assert requests[0].url.params["completed_since"] == "1970-01-01T00:00:00Z"
    assert "offset" not in requests[0].url.params
    assert requests[2].url.params["offset"] == "provider-offset"
    assert requests[4].url.params["project"] == projects[1]


async def test_multihomed_task_is_emitted_only_by_first_admitted_project():
    projects = sorted(PROJECTS)
    shared = task("101", project=projects[0])
    shared["memberships"].append({"project": {"gid": projects[1],
                                               "name": f"Area {projects[1]}"},
                                   "section": None})
    subject, _ = asana_provider(
        {"data": [{"gid": "101"}], "next_page": None},
        {"data": shared},
        {"data": [{"gid": "101"}], "next_page": None},
        {"data": shared},
    )
    first = await subject.search_work(None, None, None, 1)
    second = await subject.search_work(None, None, first.next_cursor, 1)
    assert [item.provider_work_id for item in first.items] == ["101"]
    assert second.items == ()
    assert second.next_cursor == "2:"


@pytest.mark.parametrize(
    "problem",
    ["duplicate", "disabled", "wrong_subtype", "bad_option", "wrong_option", "malformed_value"],
)
async def test_search_rejects_ineligible_or_malformed_routing_truth(problem):
    project = "9999999999999999"
    current = task("101", project=project)
    field = current["custom_fields"][0]
    if problem == "duplicate":
        current["custom_fields"].append(dict(field))
    elif problem == "disabled":
        field["enabled"] = False
    elif problem == "wrong_subtype":
        field["resource_subtype"] = "text"
    elif problem == "bad_option":
        field["enum_options"][0]["enabled"] = False
    elif problem == "wrong_option":
        field["enum_value"] = {"gid": "missing-option"}
    else:
        field["display_value"] = {"unexpected": True}
    subject, _ = asana_provider(
        {"data": [{"gid": "101"}], "next_page": None},
        {"data": current},
        test_project_gid=project,
    )
    with pytest.raises(ProviderError):
        await subject.search_work(None, None, None, 50)


@pytest.mark.parametrize(
    "text, cursor, limit",
    [(None, "", 50), (None, "x" * 1025, 50), ("", None, 50), (None, None, 101)],
)
async def test_asana_search_rejects_unbounded_provider_inputs_before_request(text, cursor, limit):
    subject, requests = asana_provider()
    with pytest.raises(ProviderError):
        await subject.search_work(text, None, cursor, limit)
    assert requests == []


async def test_asana_search_fails_closed_on_provider_failure():
    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", transport=httpx.MockTransport(fail)
    )
    with pytest.raises(ProviderError):
        await AsanaProvider(client).search_work(None, None, None, 50)


@pytest.mark.parametrize("problem", ["bad_cursor", "repeated_cursor", "too_many"])
async def test_asana_list_fails_closed_on_invalid_provider_page(problem):
    project = "9999999999999999"
    cursor, rows, next_page = None, [], None
    if problem == "bad_cursor":
        next_page = {"offset": ""}
    elif problem == "repeated_cursor":
        cursor, next_page = "0:same", {"offset": "same"}
    else:
        rows = [{"gid": "101"}, {"gid": "102"}]
    subject, _ = asana_provider(
        {"data": rows, "next_page": next_page}, test_project_gid=project
    )
    with pytest.raises(ProviderError):
        await subject.search_work(None, None, cursor, 1)


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
                    context=WorkContext(assignee="Owner"),
                ),
                ProviderSearchItem(
                    provider_work_id="202", title="Beta", completed=True,
                    revision="r2", routing=Routing(priority="P1"),
                    context=WorkContext(),
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
    assert [item.context.assignee for item in first.items] == ["Owner", None]
    assert provider.calls == [("task", None, None, 10), ("task", None, None, 10)]
    rendered = first.model_dump(mode="json")
    assert all("provider" not in item and "task_gid" not in item and "gid" not in item
               for item in rendered["items"])


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
    {"cursor": "x" * 1025},
    {"limit": 0},
    {"limit": 101},
    {"unexpected": True},
])
def test_discovery_request_contract_is_closed_and_bounded(values):
    with pytest.raises(ValidationError):
        WorkSearchRequest.model_validate({"api_version": "1"} | values)
