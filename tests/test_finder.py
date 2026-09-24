import httpx
import pytest

from switchstand.provider import PROJECT, AsanaProvider


def task(
    gid: str, *, parent: str | None = None, revision: str = "r1",
) -> dict:
    return {"data": {
        "gid": gid, "name": f"Task {gid}", "notes": "", "completed": False,
        "modified_at": revision,
        "memberships": [{"project": {"gid": PROJECT}}] if parent is None else [],
        "parent": {"gid": parent} if parent else None,
        "custom_fields": ([{"gid": "1218431623135287", "enabled": True,
                           "resource_subtype": "enum",
                           "enum_value": {"gid": "1218431623135292"},
                           "enum_options": [{"gid": "1218431623135292",
                                             "name": "Review", "enabled": True}]}]
                          if parent else []),
    }}


def provider(*responses: dict | tuple[int, dict]) -> tuple[AsanaProvider, list[httpx.Request]]:
    requests: list[httpx.Request] = []
    pending = list(responses)

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = pending.pop(0)
        status, payload = response if isinstance(response, tuple) else (200, response)
        return httpx.Response(status, json=payload)

    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", transport=httpx.MockTransport(answer)
    )
    return AsanaProvider(client), requests


async def test_direct_subtask_is_a_candidate_with_exact_relation_evidence() -> None:
    subject, requests = provider(
        task("work"),
        {"data": [{"gid": "review"}], "next_page": None},
        task("review", parent="work"),
        task("work"),
    )
    result = await subject.find_related("work")
    assert result.status == "CANDIDATES"
    assert result.observed_revision == "r1"
    assert [(candidate.task_gid, candidate.revision, candidate.parent_gid,
             candidate.work_type_option_gid) for candidate in result.candidates] == [
        ("review", "r1", "work", "1218431623135292")
    ]
    assert [request.url.path for request in requests] == [
        "/api/1.0/tasks/work", "/api/1.0/tasks/work/subtasks",
        "/api/1.0/tasks/review", "/api/1.0/tasks/work",
    ]
    assert all(request.method == "GET" for request in requests)


async def test_two_subtask_pages_use_returned_offset_and_exact_parent_evidence() -> None:
    subject, requests = provider(
        task("work"),
        {"data": [{"gid": "review"}], "next_page": {"offset": "more"}},
        task("review", parent="work"),
        {"data": [{"gid": "spec"}], "next_page": None},
        task("spec", parent="work"),
        task("work"),
    )
    result = await subject.find_related("work")
    assert result.status == "CANDIDATES"
    assert [(candidate.task_gid, candidate.parent_gid) for candidate in
            result.candidates] == [("review", "work"), ("spec", "work")]
    pages = [request for request in requests if request.url.path.endswith("/subtasks")]
    assert len(pages) == 2
    assert pages[0].url.params["limit"] == pages[1].url.params["limit"] == "20"
    assert pages[0].url.params["opt_fields"] == pages[1].url.params["opt_fields"] == "gid"
    assert "offset" not in pages[0].url.params
    assert pages[1].url.params["offset"] == "more"


async def test_duplicate_child_across_pages_is_uh_oh_with_unique_evidence() -> None:
    subject, _ = provider(
        task("work"),
        {"data": [{"gid": "review"}], "next_page": {"offset": "more"}},
        task("review", parent="work"),
        {"data": [{"gid": "review"}], "next_page": None},
        task("review", parent="work"),
        task("work"),
    )
    result = await subject.find_related("work")
    assert (result.status, result.reason) == ("UH_OH", "duplicate_subtask")
    assert [candidate.task_gid for candidate in result.candidates] == ["review"]


async def test_late_subtask_provider_failure_retains_verified_partial_evidence() -> None:
    subject, requests = provider(
        task("work"),
        {"data": [{"gid": "review"}], "next_page": {"offset": "more"}},
        task("review", parent="work"),
        (500, {"errors": [{"message": "unavailable"}]}),
    )
    result = await subject.find_related("work")
    assert (result.status, result.reason) == ("UH_OH", "read_unavailable")
    assert [candidate.task_gid for candidate in result.candidates] == ["review"]
    assert sum(request.url.path.endswith("/subtasks") for request in requests) == 2


async def test_invalid_subtask_offset_returns_uh_oh_with_partial_evidence() -> None:
    subject, requests = provider(
        task("work"),
        {"data": [{"gid": "review"}], "next_page": {"offset": ""}},
        task("review", parent="work"),
        task("work"),
    )
    result = await subject.find_related("work")
    assert (result.status, result.reason) == ("UH_OH", "invalid_subtask_offset")
    assert [candidate.task_gid for candidate in result.candidates] == ["review"]
    assert sum(request.url.path.endswith("/subtasks") for request in requests) == 1


async def test_child_cap_returns_uh_oh_with_first_100_exact_children() -> None:
    pages: list[dict] = []
    for page in range(5):
        children = [f"child-{page * 20 + number}" for number in range(20)]
        pages.append({"data": [{"gid": gid} for gid in children],
                      "next_page": {"offset": f"page-{page + 1}"}})
        pages.extend(task(gid, parent="work") for gid in children)
    subject, requests = provider(task("work"), *pages, task("work"))
    result = await subject.find_related("work")
    assert (result.status, result.reason) == ("UH_OH", "subtask_cap")
    assert len(result.candidates) == 100
    assert all(candidate.parent_gid == "work" for candidate in result.candidates)
    assert sum(request.url.path.endswith("/subtasks") for request in requests) == 5


async def test_unprojected_work_does_not_trigger_related_lookup() -> None:
    work = task("work")
    work["data"]["memberships"] = []
    subject, requests = provider(work)
    result = await subject.find_related("work")
    assert result.status == "UH_OH"
    assert result.reason == "work_not_canonical"
    assert result.candidates == ()
    assert [request.url.path for request in requests] == ["/api/1.0/tasks/work"]


@pytest.mark.parametrize("bad_revision", [None, {"invalid": True}])
async def test_missing_work_revision_is_uncertain_not_noncanonical(bad_revision: object) -> None:
    work = task("work")
    if bad_revision is None:
        del work["data"]["modified_at"]
    else:
        work["data"]["modified_at"] = bad_revision
    subject, requests = provider(work)
    result = await subject.find_related("work")
    assert (result.status, result.reason, result.candidates) == (
        "UH_OH", "work_revision_unavailable", ())
    assert [request.url.path for request in requests] == ["/api/1.0/tasks/work"]


@pytest.mark.parametrize("revision", ["r1", "r2"])
async def test_project_membership_removed_takes_priority_over_revision(revision: str) -> None:
    moved = task("work", revision=revision)
    moved["data"]["memberships"] = []
    subject, _ = provider(
        task("work"),
        {"data": [{"gid": "review"}], "next_page": None},
        task("review", parent="work"),
        moved,
    )
    result = await subject.find_related("work")
    assert result.status == "UH_OH"
    assert result.reason == "work_not_canonical"
    assert result.candidates == ()


async def test_disabled_work_type_option_is_not_role_evidence() -> None:
    review = task("review", parent="work")
    review["data"]["custom_fields"][0]["enum_options"][0]["enabled"] = False
    subject, _ = provider(
        task("work"),
        {"data": [{"gid": "review"}], "next_page": None},
        review,
        task("work"),
    )
    result = await subject.find_related("work")
    assert result.status == "CANDIDATES"
    assert result.candidates[0].work_type_option_gid is None
