import json

import httpx
import pytest

from switchstand.contracts import WorkPatch
from switchstand.core import ProviderError, UnknownEffect
from switchstand.provider import ANCESTRY_GETS, FIELDS, OPT_FIELDS, PROJECT, AsanaProvider


def field(gid=FIELDS["horizon"], *, enabled=True, option="Stage 3", display="Stage 2"):
    return {"gid": gid, "enabled": enabled, "display_value": display, "enum_options": [{"gid": "option-gid", "name": option, "enabled": enabled}]}
def task(*, parent=None, project=None, fields=()):
    memberships = [] if project is None else [{"project": {"gid": project}}]
    return {"data": {"name": "Title", "notes": "Notes", "completed": False,
                     "modified_at": "r1", "memberships": memberships,
                     "parent": None if parent is None else {"gid": parent},
                     "custom_fields": list(fields)}}
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
    ([(200, task(parent="p")), (200, task(project=PROJECT))], True, 2),
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
