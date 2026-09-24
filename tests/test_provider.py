import json

import httpx
import pytest

from switchstand.contracts import WorkContext, WorkPatch, WorkPlacement
from switchstand.core import ProviderError, UnknownEffect
from switchstand.provider import (
    ANCESTRY_GETS,
    ATTACHMENT_FIELDS,
    FIELDS,
    OPT_FIELDS,
    PROJECT,
    PROJECTS,
    ROOT_WORK_GID,
    WORK_TYPE,
    WORKSPACE,
    AsanaProvider,
)


def field(gid=FIELDS["horizon"], *, enabled=True, option="Stage 3", display="Stage 2"):
    return {"gid": gid, "enabled": enabled, "resource_subtype": "enum",
            "display_value": display, "enum_value": {"gid": "option-gid"},
            "enum_options": [{"gid": "option-gid", "name": option, "enabled": enabled}]}
def task(*, parent=None, project=None, fields=(), assignee=None):
    projects = (project if isinstance(project, tuple) else (project,)) if project else ()
    memberships = [{"project": {"gid": gid, "name": f"Area {gid}"}, "section": None}
                   for gid in projects]
    return {"data": {"name": "Title", "notes": "Notes", "completed": False,
                     "modified_at": "r1", "memberships": memberships,
                     "assignee": assignee,
                     "parent": None if parent is None else {"gid": parent},
                     "custom_fields": list(fields)}}
def candidate(gid, priority, *, completed=False, horizon="Stage 3"):
    fields = [field(FIELDS["priority"], option=priority, display=priority),
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
def provider(*responses, test_project_gid=None):
    api = API(*responses)
    client = httpx.AsyncClient(base_url="https://app.asana.com/api/1.0",
                              transport=httpx.MockTransport(api))
    return AsanaProvider(client, test_project_gid), api


TEST_PROJECT = "9999999999999999"


async def test_work_context_uses_exact_task_read_and_hides_provider_ids():
    payload = task(assignee={"gid": "user-secret", "name": "Ada"})
    payload["data"]["gid"] = "task-secret"
    payload["data"]["memberships"] = [
        {"project": {"gid": PROJECT, "name": "Zeta"},
         "section": {"gid": "section-secret", "name": "Doing"}},
        {"project": {"gid": PROJECTS[1], "name": "Alpha"}, "section": None},
        {"project": {"gid": "outside-secret", "name": "Outside"},
         "section": {"gid": "outside-section", "name": "Hidden"}},
    ]
    subject, api = provider((200, payload))

    result = await subject.get("task-secret")

    assert result is not None and result.context == WorkContext(
        assignee="Ada",
        placements=(WorkPlacement(area="Alpha"), WorkPlacement(area="Zeta", stage="Doing")),
    )
    assert len(api.requests) == 1
    assert dict(api.requests[0].url.params) == {"opt_fields": OPT_FIELDS}
    serialized = result.context.model_dump_json()
    assert all(secret not in serialized for secret in (
        "user-secret", "task-secret", "section-secret", "outside-secret", "outside-section",
    ))


@pytest.mark.parametrize("change", [
    lambda data: data.pop("assignee"),
    lambda data: data.update(assignee={"gid": "user-secret", "name": ""}),
    lambda data: data.update(memberships=[{"project": {"gid": PROJECT}}]),
    lambda data: data.update(memberships=[
        {"project": {"gid": PROJECT, "name": "Area"}, "section": None},
        {"project": {"gid": PROJECT, "name": "Changed"}, "section": None},
    ]),
])
async def test_work_context_rejects_missing_malformed_or_conflicting_truth(change):
    payload = task(project=PROJECT)
    change(payload["data"])
    subject, _ = provider((200, payload))

    with pytest.raises(ProviderError, match="provider response invalid"):
        await subject.get("123")


@pytest.mark.parametrize("malformed", [False, True])
async def test_attachment_page_is_bounded_name_only_and_validates_parent(malformed):
    row = {
        "gid": "attachment-secret", "name": "brief.txt",
        "parent": {"gid": "wrong" if malformed else "123"},
        "download_url": "https://secret.invalid/file",
    }
    subject, api = provider((200, {"data": [row], "next_page": {"offset": "next"}}))
    if malformed:
        with pytest.raises(ProviderError, match="provider request failed") as error:
            await subject.list_attachments("123", "opaque", 7)
        assert "wrong" not in str(error.value)
    else:
        result = await subject.list_attachments("123", "opaque", 7)
        assert result.attachments[0].name == "brief.txt" and result.next_cursor == "next"
    request = api.requests[0]
    assert (request.method, request.url.path) == ("GET", "/api/1.0/attachments")
    assert dict(request.url.params) == {
        "parent": "123", "limit": "7", "opt_fields": ATTACHMENT_FIELDS,
        "offset": "opaque",
    }
    assert len(api.requests) == 1


@pytest.mark.parametrize("project, allowed", [(PROJECT, False), (TEST_PROJECT, True)])
@pytest.mark.parametrize("inherited", [False, True])
async def test_test_only_admission_excludes_normal_projects(project, allowed, inherited):
    responses = ([(200, task(parent="456"))] if inherited else [])
    api = API(*responses, (200, task(project=project)))
    async with httpx.AsyncClient(base_url="https://app.asana.com/api/1.0",
                                transport=httpx.MockTransport(api)) as client:
        subject = AsanaProvider(client, TEST_PROJECT, test_only=True)
        result = await subject.get("123")
    assert result.canonical is allowed
    assert all(request.method == "GET" for request in api.requests)


def test_test_only_admission_requires_explicit_test_project():
    with pytest.raises(ValueError, match="test project"):
        AsanaProvider(None, test_only=True)


def root_field(value):
    return {"gid": ROOT_WORK_GID, "enabled": True,
            "resource_subtype": "text", "text_value": value}


async def test_grouped_lookup_requires_bound_task_to_identify_itself_as_root():
    member = task(project=PROJECT, fields=[root_field("121")])
    member["data"]["gid"] = "456"
    subject, api = provider((200, member), (200, {"data": []}), (200, member))
    result = await subject.find_grouped("456")
    assert (result.status, result.reason, result.candidates) == (
        "UH_OH", "root_identity_unverified", ()
    )
    assert [request.url.path for request in api.requests] == ["/api/1.0/tasks/456"]


async def test_grouped_lookup_discards_candidates_if_root_identity_changes_on_readback():
    root = task(project=PROJECT, fields=[root_field("121")]); root["data"]["gid"] = "121"
    changed_root = task(project=PROJECT, fields=[root_field("456")])
    changed_root["data"]["gid"] = "121"
    member = task(project=PROJECT, fields=[root_field("121")])
    member["data"]["gid"] = "456"
    subject, api = provider((200, root), (200, {"data": [{"gid": "456"}]}),
                            (200, member), (200, changed_root))
    result = await subject.find_grouped("121")
    assert (result.status, result.reason, result.candidates) == (
        "UH_OH", "root_identity_unverified", ()
    )
    assert [request.method for request in api.requests] == ["GET"] * 4


@pytest.mark.parametrize("readback", [
    (404, {"errors": [{"message": "not found"}]}),
    (500, {"errors": [{"message": "unavailable"}]}),
    (200, {"data": {"gid": "999"}}),
])
async def test_grouped_lookup_discards_candidates_when_root_readback_is_unavailable(readback):
    root = task(project=PROJECT, fields=[root_field("121")]); root["data"]["gid"] = "121"
    member = task(project=PROJECT, fields=[root_field("121")]); member["data"]["gid"] = "456"
    subject, api = provider((200, root), (200, {"data": [{"gid": "456"}]}),
                            (200, member), readback)
    result = await subject.find_grouped("121")
    assert (result.status, result.reason, result.candidates) == (
        "UH_OH", "work_readback_unavailable", ()
    )
    assert [request.method for request in api.requests] == ["GET"] * 4


@pytest.mark.parametrize("readback_field,reason,reads", [
    ("456", "root_identity_unverified", 4),
    ("121", "work_readback_unavailable", 5),
])
async def test_grouped_lookup_discards_candidates_before_or_during_readback_ancestry(
    readback_field, reason, reads,
):
    root = task(project=PROJECT, fields=[root_field("121")]); root["data"]["gid"] = "121"
    member = task(project=PROJECT, fields=[root_field("121")]); member["data"]["gid"] = "456"
    readback = task(parent="999", fields=[root_field(readback_field)])
    readback["data"]["gid"] = "121"
    subject, api = provider((200, root), (200, {"data": [{"gid": "456"}]}),
                            (200, member), (200, readback),
                            (500, {"errors": [{"message": "ancestor unavailable"}]}))
    result = await subject.find_grouped("121")
    assert (result.status, result.reason, result.candidates) == ("UH_OH", reason, ())
    assert [request.method for request in api.requests] == ["GET"] * reads


async def test_grouped_lookup_queries_exact_text_field_then_rereads_canonical_task():
    root_gid, member_gid = "121", "456"
    root = task(project=PROJECT, fields=[root_field(root_gid)])
    root["data"]["gid"] = root_gid
    member = task(project=PROJECTS[1], fields=[{
        "gid": ROOT_WORK_GID, "enabled": True, "resource_subtype": "text",
        "text_value": root_gid,
    }])
    member["data"]["gid"] = member_gid
    subject, api = provider((200, root), (200, {"data": [{"gid": member_gid}]}),
                            (200, member), (200, root))

    result = await subject.find_grouped(root_gid)

    assert result.status == "CANDIDATES" and result.complete is False
    assert result.root_task_gid == root_gid and result.observed_revision == "r1"
    assert [(row.task_gid, row.root_work_gid, row.revision, row.source) for row in result.candidates] == [
        (member_gid, root_gid, "r1", "asana_root_work_gid_search_exact_get")
    ]
    assert [request.method for request in api.requests] == ["GET"] * 4
    assert [request.url.path for request in api.requests] == [
        f"/api/1.0/tasks/{root_gid}", f"/api/1.0/workspaces/{WORKSPACE}/tasks/search",
        f"/api/1.0/tasks/{member_gid}", f"/api/1.0/tasks/{root_gid}",
    ]
    search_params = dict(api.requests[1].url.params)
    project_filter = search_params.pop("projects.any").split(",")
    assert len(project_filter) == len(PROJECTS) and set(project_filter) == set(PROJECTS)
    assert search_params == {
        f"custom_fields.{ROOT_WORK_GID}.value": root_gid,
        "limit": "100", "opt_fields": "gid",
    }


@pytest.mark.parametrize("field_value,project,expected_reason", [
    ("different", PROJECT, "relationship_changed"),
    ("121", "outside", "candidate_not_canonical"),
])
async def test_grouped_lookup_does_not_admit_changed_or_noncanonical_match(
    field_value, project, expected_reason,
):
    root = task(project=PROJECT, fields=[root_field("121")]); root["data"]["gid"] = "121"
    member = task(project=project, fields=[{
        "gid": ROOT_WORK_GID, "enabled": True, "resource_subtype": "text",
        "text_value": field_value,
    }]); member["data"]["gid"] = "456"
    subject, _ = provider((200, root), (200, {"data": [{"gid": "456"}]}), (200, member))
    result = await subject.find_grouped("121")
    assert result.status == "UH_OH" and result.reason == expected_reason
    assert not result.candidates and result.complete is False


@pytest.mark.parametrize("search_response,expected_reason", [
    ((200, {"data": []}), "no_search_matches"),
    ((200, {"data": [{"gid": "456"}, {"gid": "456"}]}), "invalid_search_row"),
    ((200, {"data": [], "next_page": {"offset": "unexpected"}}), "invalid_search_page"),
    ((402, {"errors": [{"message": "premium"}]}), "read_unavailable"),
])
async def test_grouped_lookup_preserves_unknown_for_incomplete_search(search_response, expected_reason):
    root = task(project=PROJECT, fields=[root_field("121")]); root["data"]["gid"] = "121"
    responses = [(200, root), search_response]
    if expected_reason == "no_search_matches":
        responses.append((200, root))
    subject, api = provider(*responses)
    result = await subject.find_grouped("121")
    assert result.status == "UH_OH" and result.reason == expected_reason
    assert not result.candidates and result.complete is False
    assert all(request.method == "GET" for request in api.requests)


async def test_exact_test_project_admission_and_production_only_discovery():
    subject, api = provider(
        (200, task(project=TEST_PROJECT)),
        (200, task(project="8888888888888888")),
        *[(200, page()) for _ in PROJECTS],
        test_project_gid=TEST_PROJECT,
    )
    assert (await subject.get("test-only")).canonical
    assert not (await subject.get("wrong-project")).canonical
    assert await subject.suggest_next(frozenset()) is None
    assert [request.url.path for request in api.requests] == [
        "/api/1.0/tasks/test-only", "/api/1.0/tasks/wrong-project",
        *(f"/api/1.0/projects/{gid}/tasks" for gid in PROJECTS),
    ]


async def test_test_project_ancestor_and_mixed_production_membership():
    subject, api = provider(
        (200, task(parent="parent")), (200, task(project=TEST_PROJECT)),
        test_project_gid=TEST_PROJECT,
    )
    assert (await subject.get("child")).canonical
    assert [request.url.path for request in api.requests] == [
        "/api/1.0/tasks/child", "/api/1.0/tasks/parent",
    ]
    mixed = task(project=(TEST_PROJECT, PROJECT))
    subject, _ = provider((200, mixed), (200, page(candidate("mixed", "P0"))),
                          *[(200, page()) for _ in PROJECTS[1:]])
    assert (await subject.get("mixed")).canonical
    assert (await subject.suggest_next(frozenset())).provider_work_id == "mixed"
    subject, _ = provider((200, task(project=TEST_PROJECT)))
    assert not (await subject.get("test-only")).canonical


@pytest.mark.parametrize("gid", ["abc", " 123", "123 ", "+123", "１２３", PROJECT])
def test_invalid_or_duplicate_test_project_gid_fails_closed(gid):
    with pytest.raises(ValueError, match="invalid test project GID"):
        provider(test_project_gid=gid)
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
    fields = [field(gid, option=name, display=name) for name, gid in FIELDS.items()]
    subject, _ = provider((200, task(project=PROJECT, fields=fields)))
    result = await subject.get("t"); assert result and result.routing.model_dump() == ({name: name for name in FIELDS} | {"work_type": None})
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
@pytest.mark.parametrize(("name", "gid", "value"), [("priority", FIELDS["priority"], "P0"), ("work_type", WORK_TYPE, "Implementation"), ("review_next_action", FIELDS["review_next_action"], "Code Review")])
async def test_strict_enum_mapping_minimal_update_and_readback(name, gid, value):
    before = field(gid, option=value, display="Other")
    after = field(gid, option=value, display=value)
    subject, api = provider((200, task(fields=[before])), (200, {}),
                            (200, task(project=PROJECT, fields=[after])))
    patch = {name: value} | ({"notes": "review evidence"} if name == "review_next_action" else {})
    await subject.update("t", WorkPatch(**patch))
    result = await subject.get("t")
    expected = {"custom_fields": {gid: "option-gid"}} | ({"notes": "review evidence"} if name == "review_next_action" else {})
    assert json.loads(api.requests[1].content) == {"data": expected}
    assert result and getattr(result.routing, name) == value

@pytest.mark.parametrize("mutate", [
    lambda fields: fields.clear(),
    lambda fields: fields[0].update(enabled=False),
    lambda fields: fields[0].update(resource_subtype="text"),
    lambda fields: fields[0]["enum_options"].append("malformed"),
    lambda fields: fields[0]["enum_options"][0].update(gid=""),
    lambda fields: fields[0]["enum_options"][0].update(gid=7),
    lambda fields: fields[0]["enum_options"][0].update(enabled=False),
    lambda fields: fields[0]["enum_options"][0].update(name="Other"),
    lambda fields: fields[0]["enum_options"].append(
        {"gid": "duplicate", "name": fields[0]["enum_options"][0]["name"], "enabled": True}),
    lambda fields: fields.append(fields[0].copy()),
])
@pytest.mark.parametrize(("name", "gid", "value"), [("priority", FIELDS["priority"], "P0"), ("work_type", WORK_TYPE, "Implementation"), ("review_next_action", FIELDS["review_next_action"], "Code Review")])
async def test_invalid_strict_enum_catalogue_denies_before_put(mutate, name, gid, value):
    fields = [field(gid, option=value)]
    mutate(fields)
    subject, api = provider((200, task(fields=fields)))
    with pytest.raises(ProviderError, match="routing write denied"):
        await subject.update("t", WorkPatch(**({name: value} | ({"notes": "evidence"} if name == "review_next_action" else {}))))
    assert len(api.requests) == 1

@pytest.mark.parametrize("fields", [
    [field(FIELDS["priority"]), field(FIELDS["priority"])],
    [{"gid": FIELDS["priority"], "enabled": True, "display_value": "P0"}],
    [field(FIELDS["priority"], option="P0", display="P0")],
])
async def test_invalid_priority_readback_is_rejected(fields):
    if len(fields) == 1 and fields[0].get("enum_options"):
        fields[0]["enum_options"].append({"gid": "broken"})
    subject, _ = provider((200, task(project=PROJECT, fields=fields)))
    with pytest.raises(ProviderError, match="provider response invalid"):
        await subject.get("t")

async def test_review_next_action_accepts_unset_and_rejects_partial_truth():
    subject, _ = provider((200, task(project=PROJECT)))
    assert (await subject.get("t")).routing.review_next_action is None
    unset = field(FIELDS["review_next_action"]); unset.update(display_value=None, enum_value=None)
    subject, _ = provider((200, task(project=PROJECT, fields=[unset])))
    result = await subject.get("t")
    assert result and result.routing.review_next_action is None
    for display, selected in (("Code Review", None), (None, {"gid": "option-gid"})):
        partial = field(FIELDS["review_next_action"], option="Code Review")
        partial.update(display_value=display, enum_value=selected)
        subject, _ = provider((200, task(project=PROJECT, fields=[partial])))
        with pytest.raises(ProviderError, match="provider response invalid"):
            await subject.get("t")
    malformed = field(FIELDS["review_next_action"]); malformed["enum_options"].append({"gid": "broken"})
    malformed_selected = field(FIELDS["review_next_action"])
    malformed_selected.update(display_value=None, enum_value={"gid": 7})
    duplicate_selected = field(FIELDS["review_next_action"], option="Code Review",
                               display="Code Review")
    duplicate_selected["enum_options"].append(
        {"gid": "option-gid", "name": "Other", "enabled": True})
    invalid = [
        [malformed],
        [malformed_selected],
        [duplicate_selected],
        [field(FIELDS["review_next_action"]), field(FIELDS["review_next_action"])],
        [field(FIELDS["review_next_action"], enabled=False)],
        [field(FIELDS["review_next_action"], option="Code Review", display="Other")],
        [field(FIELDS["review_next_action"], option="Code Review", display="Code Review")],
    ]
    invalid[-1][0]["enum_value"] = {"gid": "wrong"}
    for fields in invalid:
        subject, _ = provider((200, task(project=PROJECT, fields=fields)))
        with pytest.raises(ProviderError, match="provider response invalid"):
            await subject.get("t")

async def test_only_priority_rejects_malformed_option_sibling():
    malformed = {"gid": "broken"}
    horizon = field(FIELDS["horizon"], display="Stage 2")
    horizon["enum_options"].append(malformed)
    subject, api = provider((200, task(fields=[horizon])), (200, {}))
    await subject.update("t", WorkPatch(notes="reason", horizon="Stage 3"))
    assert json.loads(api.requests[1].content) == {"data": {"notes": "reason",
        "custom_fields": {FIELDS["horizon"]: "option-gid"}}}

    priority = field(FIELDS["priority"], option="P0")
    priority["enum_options"].append(malformed)
    subject, api = provider((200, task(fields=[priority])))
    with pytest.raises(ProviderError, match="routing write denied"):
        await subject.update("t", WorkPatch(notes="reason", priority="P0"))
    assert len(api.requests) == 1
async def test_malformed_work_type_does_not_poison_other_reads_or_writes():
    broken = field(WORK_TYPE, option="Implementation", display="Implementation"); broken["enum_options"].append({"gid": "broken"}); fields = [field(FIELDS["priority"], option="P0", display="P0"), field(FIELDS["horizon"], display="Stage 3"), broken]; subject, api = provider((200, task(project=PROJECT, fields=fields)), (200, {}))
    result = await subject.get("t"); await subject.update("t", WorkPatch(completed=True)); assert result and (result.routing.priority, result.routing.horizon, result.routing.work_type) == ("P0", "Stage 3", None) and not result.completed
    assert json.loads(api.requests[1].content) == {"data": {"completed": True}}
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
