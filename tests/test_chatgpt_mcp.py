import sys
from pathlib import Path
from uuid import uuid4

import pytest
from chatgpt_fixture import ACTIVE, PRINCIPAL, REFERENCE, assert_public, grant, read_chain, service
from mcp import Client, StdioServerParameters
from pydantic import ValidationError

from switchstand.chatgpt_mcp import build_chatgpt_server
from switchstand.contracts import SourceTaskRequest
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


async def test_related_get_keeps_grant_guard_and_exact_bound_source():
    subject = service()
    provider = subject.providers["asana"]
    denied = await subject.get(uuid4(), include_related=True)
    assert denied.status == "denied" and provider.related_calls == []
    current = await subject.get(ACTIVE, include_related=True)
    assert current.status == "ok" and current.related is not None
    assert current.related.candidates[0].parent_gid == "123"
    assert provider.related_calls == ["123"]


async def test_workspace_search_requires_explicit_operation_and_returns_only_work_ids():
    subject = service()
    subject.grants.grant = grant(
        scope="launch",
        operations=frozenset({"work_get", "work_search"}),
        append_qualification=None,
    )
    denied = await build_chatgpt_server(subject).call_tool(
        "work_search", {"api_version": "1"}
    )
    assert denied.structured_content["status"] == "denied"
    assert subject.providers["asana"].search_calls == []

    subject.grants.grant = grant(
        scope="workspace",
        operations=frozenset({"work_get", "work_search"}),
        append_qualification=None,
    )
    result = await build_chatgpt_server(subject).call_tool(
        "work_search", {"api_version": "1", "text": "Task", "limit": 10}
    )
    value = result.structured_content
    assert value["status"] == "ok" and len(value["items"]) == 1
    assert value["items"][0]["title"] == "Task"
    assert "provider" not in value["items"][0] and "task_gid" not in value["items"][0]
    assert subject.providers["asana"].search_calls == [("Task", None, None, 10)]

    subject.grants.grant = grant(
        scope="workspace", operations=frozenset({"work_get"}), append_qualification=None,
    )
    denied = await build_chatgpt_server(subject).call_tool(
        "work_search", {"api_version": "1"}
    )
    assert denied.structured_content["status"] == "denied"
    assert subject.providers["asana"].search_calls == [("Task", None, None, 10)]


async def test_real_stdio_surface_has_no_issuer_or_identity_argument():
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__)), "serve"], env={"PYTHONPATH": str(Path.cwd() / "src")})
    async with Client(parameters) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == {
            "grant_get", "work_get", "work_search", "source_task", "source_stories",
            "source_story", "work_history", "work_attachments", "work_event", "work_append",
            "work_create", "message_send", "message_pending",
        }
        for tool in tools:
            if tool.name in {"work_get", "work_history", "work_attachments", "work_event"}:
                assert_public(tool.model_dump(mode="json"))
            if tool.name == "work_attachments":
                assert tool.input_schema["properties"]["observed_revision"]["minLength"] == 1
            assert tool.input_schema.get("additionalProperties") is False
            assert not {"principal", "role", "grant_id", "issuer", "allowed_operations"}.intersection(
                tool.input_schema.get("properties", {}))
        introspection = (await client.call_tool("grant_get", {"api_version": "1"})).structured_content
        assert introspection["principal"] == PRINCIPAL.model_dump(mode="json")
        got = (await client.call_tool("work_get", {"api_version": "1"})).structured_content
        assert got["item"]["id"] == str(ACTIVE)
        search = (await client.call_tool(
            "work_search", {"api_version": "1", "text": "Task"}
        )).structured_content
        assert search["status"] == "denied" and search["items"] == []
        related = (await client.call_tool("work_get", {
            "api_version": "1", "include_related": True,
        })).structured_content
        assert related["related"]["candidates"] == [{"title": "Review", "revision": "r1"}]
        bad = await client.call_tool("work_get", {"api_version": "1", "role": "owner"})
        assert bad.is_error
        args = {'api_version': "1", 'operation_id': str(uuid4()), 'work_id': str(ACTIVE), 'grant_version': 1, 'observed_revision': "r1", 'text': "protocol feedback"}
        reference = await client.call_tool("work_append", args | {"work_id": str(REFERENCE)})
        assert reference.structured_content["status"] == "denied"
        first = (await client.call_tool("work_append", args)).structured_content
        assert first["status"] == "ok" and first["receipt"]["task_gid"] == "123"
        assert (await client.call_tool("work_append", args)).structured_content == first


async def test_workspace_read_chain_and_causal_denials(monkeypatch):
    from unittest.mock import AsyncMock

    from switchstand.core import ProviderSourceStory

    subject = service()
    subject.grants.grant = grant(scope="workspace", operations=frozenset({"work_get", "work_search"}))
    provider = subject.providers["asana"]
    provider.stories = [ProviderSourceStory("raw-event", "123", "comment_added", "history", "now", "Marco")]
    server = build_chatgpt_server(subject)
    found = await server.call_tool("work_search", {"api_version": "1"})
    assert_public(found.model_dump(mode="json"))
    await read_chain(server, found.structured_content["items"][0]["id"])
    for selected, target in ((grant(scope="workspace", operations=frozenset({"work_search"})), ACTIVE),
                             (grant(scope="launch"), uuid4())):
        subject.grants.grant = selected
        with monkeypatch.context() as patch:
            for method in ("get", "source_task", "source_stories", "source_story", "find_related"):
                patch.setattr(provider, method, AsyncMock(side_effect=AssertionError("unauthorized access")))
            for tool, extra in (("work_get", {}), ("work_history", {"observed_revision": "r1"}),
                                ("work_event", {"observed_revision": "r1", "event_id": str(uuid4())})):
                denied = await server.call_tool(tool, {"api_version": "1", "work_id": str(target), **extra})
                assert_public(denied.model_dump(mode="json"))
                assert denied.structured_content["status"] == "denied"


if __name__ == "__main__":
    build_chatgpt_server(service()).run()
