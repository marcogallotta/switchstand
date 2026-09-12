import json

import httpx
import pytest

from switchstand.contracts import WorkPatch
from switchstand.core import ProviderError, UnknownEffect
from switchstand.provider import ANCESTRY_GETS, FIELDS, OPT_FIELDS, PROJECT, PROJECTS, AsanaProvider


def field(gid=FIELDS["horizon"], *, enabled=True, option="Stage 3", display="Stage 2"):
    return {"gid": gid, "enabled": enabled, "display_value": display, "enum_options": [{"gid": "option-gid", "name": option, "enabled": enabled}]}
def task(*, parent=None, project=None, fields=()):
    memberships = [] if project is None else [{"project": {"gid": project}}]
    return {"data": {"name": "Title", "notes": "Notes", "completed": False,
                     "modified_at": "r1", "memberships": memberships,
                     "parent": None if parent is None else {"gid": parent},
                     "custom_fields": list(fields)}}
def candidate(gid, priority, *, completed=False, horizon="Stage 3"):
    fields = [field(FIELDS["priority"], display=priority),
              field(FIELDS["horizon"], display=horizon)]
    return {"gid": gid, "name": f"Work {gid}", "completed": completed,
            "custom_fields": fields}
def page(*candidates):
    return {"data": list(candidates), "next_page": None}
class API:
    def __init__(self, *responses): self.responses, self.requests = list(responses), []
    def __call__(self, request):
        self.requests.append(request); response = self.responses.pop(0)
        if isinstance(response, Exception): raise response
        return httpx.Response(response[0], json=response[1])
def provider(*responses):
    api = API(*responses)
    client = httpx.AsyncClient(base_url="https://app.asana.com/api/1.0",
                              transport=httpx.MockTransport(api))
    return AsanaProvider(client), api
@pytest.mark.parametrize("responses,canonical,count", [
    ([(200, task(project=PROJECT))], True, 1),
    ([(200, task(project=PROJECTS[1]))], True, 1),
    ([(200, task(parent="p")), (200, task(project=PROJECTS[-1]))], True, 2),
    ([(200, task(project="unapproved"))], False, 1),
    ([(200, task())], False, 1),
])
async def test_exact_get_and_effective_membership(responses, canonical, count):
    subject, api = provider(*responses); result = await subject.get("t")
    assert result and result.canonical is canonical and len(api.requests) == count
    assert api.requests[0].url.path == "/api/1.0/tasks/t"
    assert api.requests[0].url.params["opt_fields"] == OPT_FIELDS
async def test_unknown_task_and_routing_projection():
    subject, _ = provider((404, {})); assert await subject.get("missing") is None
    fields = [field(gid, display=name) for name, gid in FIELDS.items()]
    subject, _ = provider((200, task(project=PROJECT, fields=fields)))
    result = await subject.get("t"); assert result and result.routing.model_dump() == {name: name for name in FIELDS}
def test_project_registry_does_not_expand_writable_routing_fields():
    assert FIELDS == {
        "priority": "1217653169990249",
        "horizon": "1218212397743203",
        "review_next_action": "1218212397743210",
        "stage3_gate": "1218212397743217",
    }
async def test_ancestry_is_bounded():
    subject, api = provider(*[(200, task(parent=str(i))) for i in range(ANCESTRY_GETS)])
    result = await subject.get("t"); assert result and not result.canonical and len(api.requests) == ANCESTRY_GETS
async def test_routing_mapping_minimal_update_and_readback():
    before, after = field(), field(display="Stage 3")
    subject, api = provider((200, task(fields=[before])), (200, {}),
                            (200, task(project=PROJECT, fields=[after])))
    await subject.update("t", WorkPatch(notes="new", horizon="Stage 3"))
    result = await subject.get("t")
    assert (api.requests[1].method, api.requests[1].url.path) == ("PUT", "/api/1.0/tasks/t")
    assert json.loads(api.requests[1].content) == {"data": {"notes": "new", "custom_fields": {
        FIELDS["horizon"]: "option-gid"}}}
    assert result and result.routing.horizon == "Stage 3"
async def test_update_is_narrow():
    subject, api = provider((200, {})); await subject.update("t", WorkPatch(completed=True))
    assert api.requests[0].method == "PUT" and json.loads(api.requests[0].content) == {
        "data": {"completed": True}}
async def test_update_ambiguous_response_is_unknown_and_not_retried():
    error = httpx.ReadTimeout("lost", request=httpx.Request("PUT", "https://a"))
    subject, api = provider(error)
    with pytest.raises(UnknownEffect, match="provider effect unknown"):
        await subject.update("t", WorkPatch(notes="x"))
    assert len(api.requests) == 1
    subject, api = provider((500, {}))
    with pytest.raises(UnknownEffect, match="provider effect unknown"):
        await subject.update("t", WorkPatch(notes="x"))
    assert len(api.requests) == 1
    subject, api = provider((400, {}))
    with pytest.raises(ProviderError, match="provider write failed"):
        await subject.update("t", WorkPatch(notes="x"))
    assert len(api.requests) == 1
@pytest.mark.parametrize("fields", [[], [field(enabled=False)], [field(), field()],
                                     [field(option="Other")]])
async def test_bad_routing_settings_deny_without_put(fields):
    subject, api = provider((200, task(fields=fields)))
    with pytest.raises(ProviderError, match="routing write denied"):
        await subject.update("t", WorkPatch(notes="new", horizon="Stage 3"))
    assert len(api.requests) == 1
async def test_append_once_and_ambiguous_response_is_not_retried():
    subject, api = provider((201, {"data": {"gid": "s"}})); assert await subject.append("t", "x")
    assert (api.requests[0].method, api.requests[0].url.path) == ("POST", "/api/1.0/tasks/t/stories")
    assert json.loads(api.requests[0].content) == {"data": {"text": "x"}}
    subject, _ = provider((201, {"data": {}})); assert not await subject.append("t", "x")
    error = httpx.ReadTimeout("lost", request=httpx.Request("POST", "https://a"))
    subject, api = provider(error)
    with pytest.raises(UnknownEffect, match="provider effect unknown"): await subject.append("t", "x")
    assert len(api.requests) == 1
    subject, _ = provider((500, {}))
    with pytest.raises(UnknownEffect): await subject.append("t", "x")
    subject, _ = provider((400, {}))
    with pytest.raises(ProviderError): await subject.append("t", "x")
async def test_failures_are_sanitized():
    subject, _ = provider((500, {"errors": [{"message": "secret"}]}))
    with pytest.raises(ProviderError) as read_error: await subject.get("t")
    subject, _ = provider((500, {"errors": [{"message": "secret"}]}))
    with pytest.raises(ProviderError) as write_error: await subject.update("t", WorkPatch(notes="x"))
    malformed = task(project=PROJECT); malformed["data"]["notes"] = {"secret": True}
    with pytest.raises(ProviderError) as malformed_error: await provider((200, malformed))[0].get("t")
    assert "secret" not in str(read_error.value) + str(write_error.value) + str(malformed_error.value)

async def test_suggest_next_returns_only_highest_priority_actionable_head():
    payloads = [page(candidate("bound", "P0"), candidate("later", "P2")),
                page(), page(candidate("head", "P1"), candidate("unset", "UNSET"))]
    payloads.extend(page() for _ in PROJECTS[len(payloads):])
    subject, api = provider(*[(200, payload) for payload in payloads])
    result = await subject.suggest_next(frozenset({"bound"}))
    assert result and (result.provider_work_id, result.title, result.priority) == (
        "head", "Work head", "P1")
    assert [request.url.path for request in api.requests] == [
        f"/api/1.0/projects/{project}/tasks" for project in PROJECTS
    ]
    assert all(request.url.params["limit"] == "100" for request in api.requests)

async def test_suggest_next_discovers_area_only_task_without_exact_id():
    payloads = [page() for _ in PROJECTS]
    payloads[2] = page(candidate("area-only-fixture", "P0"))
    subject, _ = provider(*[(200, payload) for payload in payloads])
    result = await subject.suggest_next(frozenset())
    assert result and result.provider_work_id == "area-only-fixture"

async def test_suggest_next_deduplicates_cross_project_membership_by_identity():
    payloads = [page() for _ in PROJECTS]
    payloads[0] = page(candidate("shared", "P2"))
    payloads[1] = page(candidate("shared", "P0"))
    subject, _ = provider(*[(200, payload) for payload in payloads])
    result = await subject.suggest_next(frozenset())
    assert result and (result.provider_work_id, result.priority) == ("shared", "P2")

async def test_suggest_next_fails_closed_on_truncated_provider_page():
    subject, _ = provider((200, {"data": [candidate("head", "P0")],
                                 "next_page": {"offset": "more"}}))
    with pytest.raises(ProviderError, match="provider request failed"):
        await subject.suggest_next(frozenset())

async def test_suggest_next_fails_closed_on_malformed_actionable_row():
    malformed = candidate("broken", "P0")
    malformed["name"] = None
    subject, _ = provider((200, page(malformed, candidate("lower", "P1"))))
    with pytest.raises(ProviderError, match="provider request failed"):
        await subject.suggest_next(frozenset())


def source_task_payload(*, gid="123", revision="r1", canonical=True):
    payload = task(project=PROJECT if canonical else None)
    payload["data"].update(gid=gid, modified_at=revision)
    return payload


def source_story_payload(*, gid="456", target="123"):
    return {"gid": gid, "target": {"gid": target}, "resource_subtype": "comment_added",
            "text": "feedback", "created_at": "2026-09-12T00:00:00Z",
            "created_by": {"name": "Marco"}}


async def test_source_identity_and_canonical_ancestry():
    child = source_task_payload()
    child["data"].update(memberships=[], parent={"gid": "789"})
    subject, api = provider((200, child), (200, source_task_payload(gid="789")))
    result = await subject.source_task("123")
    assert result and result.canonical and len(api.requests) == 2
    subject, _ = provider((200, source_task_payload(gid="999")))
    with pytest.raises(ProviderError):
        await subject.source_task("123")


async def test_history_page_preserves_exact_cursor_and_revision():
    snapshot = source_task_payload()
    page = {"data": [source_story_payload()], "next_page": {"offset": "next"}}
    subject, api = provider((200, snapshot), (200, page), (200, snapshot))
    result = await subject.source_stories("123", "r1", "prior", 1)
    assert result and not result.stale and result.next_offset == "next"
    assert result.stories[0].story_gid == "456" and result.stories[0].task_gid == "123"
    request = api.requests[1]
    assert request.url.path == "/api/1.0/tasks/123/stories"
    assert request.url.params["offset"] == "prior" and request.url.params["limit"] == "1"
    assert len(api.requests) == 3


@pytest.mark.parametrize("canonical,revision", [(True, "r2"), (False, "r1")])
async def test_changed_or_noncanonical_history_does_not_fetch_stories(canonical, revision):
    subject, api = provider((200, source_task_payload(canonical=canonical, revision=revision)))
    page = await subject.source_stories("123", "r1", None, 50)
    assert page and not page.stories and page.canonical is canonical
    assert page.stale is canonical and len(api.requests) == 1


@pytest.mark.parametrize("canonical,revision", [(True, "r2"), (False, "r1")])
async def test_history_discards_page_when_task_changes_during_read(canonical, revision):
    subject, api = provider(
        (200, source_task_payload()),
        (200, {"data": [source_story_payload()], "next_page": None}),
        (200, source_task_payload(canonical=canonical, revision=revision)),
    )
    page = await subject.source_stories("123", "r1", None, 50)
    assert page and not page.stories and page.next_offset is None
    assert page.canonical is canonical and page.stale is canonical
    assert len(api.requests) == 3


@pytest.mark.parametrize("payload", [
    {"data": [source_story_payload(target="999")], "next_page": None},
    {"data": [source_story_payload() | {"target": None}], "next_page": None},
    {"data": [None], "next_page": None},
    {"data": [], "next_page": {}},
    {"data": [], "next_page": {"offset": ""}},
    {"data": [], "next_page": "more"},
    {"data": [], "next_page": {"offset": "prior"}},
    {"data": []},
    {"data": [source_story_payload(), source_story_payload(gid="457")], "next_page": None},
])
async def test_invalid_history_never_claims_complete_or_returns_wrong_task(payload):
    subject, api = provider((200, source_task_payload()), (200, payload))
    with pytest.raises(ProviderError):
        await subject.source_stories("123", "r1", "prior", 1)
    assert len(api.requests) == 2


async def test_exact_story_rereads_current_text_and_validates_identity():
    subject, api = provider((200, {"data": source_story_payload() | {"text": "edited"}}))
    result = await subject.source_story("123", "456")
    assert result and result.text == "edited" and result.task_gid == "123"
    assert api.requests[0].url.path == "/api/1.0/stories/456"
    subject, _ = provider((200, {"data": source_story_payload(gid="999")}))
    with pytest.raises(ProviderError):
        await subject.source_story("123", "456")
    subject, _ = provider((404, {}))
    assert await subject.source_story("123", "456") is None
