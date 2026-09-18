import sys
from pathlib import Path
from uuid import uuid4

import pytest
from chatgpt_fixture import ACTIVE, PRINCIPAL, REFERENCE, grant, service
from mcp import Client, StdioServerParameters
from pydantic import ValidationError

from switchstand.chatgpt_mcp import build_chatgpt_server
from switchstand.contracts import SourceTaskRequest, WorkSearchRequest
from switchstand.grants import PrincipalContext, ProtectedAppend, ProtectedCreate


@pytest.mark.parametrize("field", ["principal", "role", "grant", "allowed_operations"])
def test_append_cannot_accept_authority_arguments(field):
    values = {'api_version': "1", 'operation_id': uuid4(), 'work_id': ACTIVE, 'grant_version': 1, 'observed_revision': "r1", 'text': "feedback"}
    with pytest.raises(ValidationError):
        ProtectedAppend.model_validate(values | {field: "owner"})
    create = {'api_version': "1", 'operation_id': uuid4(), 'parent_work_id': ACTIVE,
              'grant_version': 1, 'title': "child"}
    with pytest.raises(ValidationError):
        ProtectedCreate.model_validate(create | {field: "owner"})


async def test_broad_reads_survive_missing_revoked_or_unqualified_write_grant():
    subject = service()
    for selected in (None, grant(state="revoked"), grant(append_qualification=None)):
        subject.grants.grant = selected
        result = await subject.source_task(SourceTaskRequest(api_version="1", task_gid="123"))
        assert result.status == "ok" and result.item.notes == "initial notes"
        denied = await subject.append(ProtectedAppend(api_version="1", operation_id=uuid4(),
            work_id=ACTIVE, grant_version=1, observed_revision="r1", text="feedback"))
        assert denied.status == "denied" and denied.effect == "not_sent"
    assert subject.providers["asana"].sends == 0


async def test_each_call_resolves_the_caller_again_and_does_not_self_take():
    subject = service()
    assert (await subject.get()).status == "ok"

    async def another():
        return PrincipalContext(issuer="fixture", subject="reviewer", client_id="local-test",
                                assurance="test")
    subject.principal = another
    assert (await subject.get(ACTIVE)).status == "denied"
    assert (await subject.grant_get()).status == "denied"
    assert (await subject.source_task(SourceTaskRequest(api_version="1", task_gid="123"))).status == "ok"

    async def absent():
        return None
    subject.principal = absent
    assert (await subject.source_task(SourceTaskRequest(api_version="1", task_gid="123"))).status == "denied"


async def test_workspace_search_binds_stable_work_and_launch_scope_is_denied():
    subject = service()
    request = WorkSearchRequest(api_version="1", text="discover")

    denied = await subject.search(request)
    assert denied.status == "denied"

    subject.grants.grant = grant(
        scope="workspace",
        operations=frozenset({"work_get", "work_search"}),
    )
    first = await subject.search(request)
    second = await subject.search(request)
    assert first.status == second.status == "ok"
    assert len(first.items) == 1
    assert first.items[0].id == second.items[0].id
    assert first.items[0].title == "Discovered"
    rendered = first.model_dump(mode="json")
    assert "789" not in str(rendered)
    assert "provider" not in str(rendered)

    discovered = await subject.get(first.items[0].id)
    assert discovered.status == "ok"
    assert discovered.item is not None and discovered.item.id == first.items[0].id


async def test_related_get_keeps_grant_guard_and_exact_bound_source():
    subject = service()
    provider = subject.providers["asana"]
    denied = await subject.get(uuid4(), include_related=True)
    assert denied.status == "denied" and provider.related_calls == []
    current = await subject.get(ACTIVE, include_related=True)
    assert current.status == "ok" and current.related is not None
    assert current.related.candidates[0].parent_gid == "123"
    assert provider.related_calls == ["123"]


async def test_real_stdio_surface_has_no_issuer_or_identity_argument():
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__)), "serve"], env={"PYTHONPATH": str(Path.cwd() / "src")})
    async with Client(parameters) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == {
            "grant_get", "work_get", "work_search", "source_task", "source_stories",
            "source_story", "work_append", "work_create",
        }
        for tool in tools:
            assert tool.input_schema.get("additionalProperties") is False
            assert not {"principal", "role", "grant_id", "issuer", "allowed_operations"}.intersection(
                tool.input_schema.get("properties", {}))
        introspection = (await client.call_tool("grant_get", {"api_version": "1"})).structured_content
        assert introspection["principal"] == PRINCIPAL.model_dump(mode="json")
        got = (await client.call_tool("work_get", {"api_version": "1"})).structured_content
        assert got["item"]["id"] == str(ACTIVE)
        related = (await client.call_tool("work_get", {
            "api_version": "1", "include_related": True,
        })).structured_content
        assert related["related"]["candidates"][0]["parent_gid"] == "123"
        denied_search = await client.call_tool(
            "work_search", {"api_version": "1", "text": "discover"}
        )
        assert denied_search.structured_content["status"] == "denied"
        bad = await client.call_tool("work_get", {"api_version": "1", "role": "owner"})
        assert bad.is_error
        args = {'api_version': "1", 'operation_id': str(uuid4()), 'work_id': str(ACTIVE), 'grant_version': 1, 'observed_revision': "r1", 'text': "protocol feedback"}
        reference = await client.call_tool("work_append", args | {"work_id": str(REFERENCE)})
        assert reference.structured_content["status"] == "denied"
        first = (await client.call_tool("work_append", args)).structured_content
        assert first["status"] == "ok" and first["receipt"]["task_gid"] == "123"
        assert (await client.call_tool("work_append", args)).structured_content == first


if __name__ == "__main__":
    build_chatgpt_server(service()).run()
