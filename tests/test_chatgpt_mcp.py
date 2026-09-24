import sys
from pathlib import Path
from uuid import uuid4

import pytest
from chatgpt_fixture import (
    ACTIVE,
    CONTEXT,
    PRINCIPAL,
    REFERENCE,
    assert_public,
    grant,
    read_chain,
    service,
)
from mcp import Client, StdioServerParameters
from pydantic import ValidationError

from switchstand.chatgpt_mcp import build_chatgpt_server
from switchstand.contracts import Routing, SourceTaskRequest, WorkResolveReferenceRequest
from switchstand.core import ProviderError
from switchstand.discovery import ProviderSearchItem, ProviderStructure
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
    assert value["items"][0]["context"] == {
        "assignee": "Ada", "placements": [{"area": "Engineering", "stage": "Doing"}],
    }
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


async def test_structure_requires_workspace_read_and_discovery_and_projects_atomically(monkeypatch):
    from unittest.mock import AsyncMock

    subject = service()
    provider = subject.providers["asana"]
    relation = ProviderSearchItem(
        "456", "Parent", False, "p1", Routing(priority="P1"), CONTEXT,
    )
    structure = AsyncMock(return_value=ProviderStructure("ok", "r1", relation, ()))
    monkeypatch.setattr(provider, "structure_work", structure, raising=False)
    server = build_chatgpt_server(subject)
    for selected in (
        grant(scope="launch", operations=frozenset({"work_get", "work_search"})),
        grant(scope="workspace", operations=frozenset({"work_get"})),
    ):
        subject.grants.grant = selected
        denied = await server.call_tool("work_structure", {
            "api_version": "1", "work_id": str(ACTIVE), "observed_revision": "r1",
        })
        assert denied.structured_content["status"] == "denied"
    structure.assert_not_awaited()
    subject.grants.grant = grant(
        scope="workspace", operations=frozenset({"work_get", "work_search"}),
    )
    result = await server.call_tool("work_structure", {
        "api_version": "1", "work_id": str(ACTIVE), "observed_revision": "r1",
    })
    value = result.structured_content
    assert value["status"] == "ok" and value["parent"]["id"] == str(REFERENCE)
    assert "provider" not in str(value).lower() and "456" not in str(value)
    structure.assert_awaited_once_with("123", "r1")

    structure.return_value = ProviderStructure("stale", "r2")
    stale = await server.call_tool("work_structure", {
        "api_version": "1", "work_id": str(ACTIVE), "observed_revision": "r1",
    })
    assert stale.structured_content == {
        "status": "stale", "work_id": str(ACTIVE), "revision": "r2",
        "parent": None, "children": [],
    }
    structure.side_effect = ProviderError("private detail")
    failed = await server.call_tool("work_structure", {
        "api_version": "1", "work_id": str(ACTIVE), "observed_revision": "r1",
    })
    assert failed.structured_content["status"] == "provider_error"
    assert "private detail" not in str(failed)


@pytest.mark.parametrize("parent_id, child_id", [
    ("123", "child"),
    ("same", "same"),
])
async def test_structure_invalid_snapshot_returns_no_structure_or_bindings(
    monkeypatch, parent_id, child_id,
):
    from unittest.mock import AsyncMock

    subject = service()
    subject.grants.grant = grant(
        scope="workspace", operations=frozenset({"work_get", "work_search"}),
    )

    def relation(provider_work_id):
        return ProviderSearchItem(
            provider_work_id, provider_work_id, False, "r1", Routing(), CONTEXT,
        )

    monkeypatch.setattr(
        subject.providers["asana"], "structure_work",
        AsyncMock(return_value=ProviderStructure(
            "ok", "r1", relation(parent_id), (relation(child_id),)
        )),
        raising=False,
    )
    before = dict(subject.state.handles)

    result = await build_chatgpt_server(subject).call_tool("work_structure", {
        "api_version": "1", "work_id": str(ACTIVE), "observed_revision": "r1",
    })

    assert result.structured_content == {
        "status": "provider_error", "work_id": None, "revision": None,
        "parent": None, "children": [],
    }
    assert subject.state.handles == before


async def test_launch_reference_denies_unbound_canonical_task_without_binding(monkeypatch):
    from unittest.mock import AsyncMock

    subject = service()
    provider = subject.providers["asana"]
    provider.canonical_ids.add("789")
    before = dict(subject.state.handles)
    with monkeypatch.context() as patch:
        get = AsyncMock(side_effect=AssertionError("launch denial must precede provider read"))
        patch.setattr(provider, "get", get)
        result = await subject.resolve_reference(
            WorkResolveReferenceRequest(api_version="1", reference="789")
        )
    assert result.status == "denied"
    assert subject.state.handles == before
    get.assert_not_awaited()


async def test_workspace_reference_binds_once_revalidates_and_stays_provider_neutral():
    subject = service()
    subject.grants.grant = grant(
        scope="workspace", operations=frozenset({"work_get"}), append_qualification=None,
    )
    provider = subject.providers["asana"]
    provider.canonical_ids.add("789")
    server = build_chatgpt_server(subject)

    first = await server.call_tool("work_resolve_reference", {
        "api_version": "1", "reference": "https://app.asana.com/0/42/789/f",
    })
    assert_public(first.model_dump(mode="json"))
    first_item = first.structured_content["item"]
    assert first.structured_content["status"] == "ok" and first_item["title"] == "Task"
    assert len(subject.state.handles) == 3

    repeat = await server.call_tool(
        "work_resolve_reference", {"api_version": "1", "reference": "789"}
    )
    assert repeat.structured_content["item"]["id"] == first_item["id"]
    assert len(subject.state.handles) == 3

    provider.canonical_ids.remove("789")
    denied = await server.call_tool(
        "work_resolve_reference", {"api_version": "1", "reference": "789"}
    )
    assert denied.structured_content == {
        "status": "denied", "item": None, "related": None, "grouped": None, "guard": None,
    }


async def test_reference_rejects_unrecognized_syntax_without_provider_read(monkeypatch):
    from unittest.mock import AsyncMock

    subject = service()
    get = AsyncMock(side_effect=AssertionError("invalid syntax must be closed before provider read"))
    monkeypatch.setattr(subject.providers["asana"], "get", get)
    result = await subject.resolve_reference(WorkResolveReferenceRequest(
        api_version="1", reference="https://example.com/task/123",
    ))
    assert result.status == "unknown" and result.item is None
    get.assert_not_awaited()


async def test_real_stdio_surface_has_no_issuer_or_identity_argument():
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__)), "serve"], env={"PYTHONPATH": str(Path.cwd() / "src")})
    async with Client(parameters) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == {
            "grant_get", "work_get", "work_search", "work_resolve_reference", "work_structure",
            "source_task", "source_stories",
            "source_story", "work_history", "work_attachments", "work_event", "work_append",
            "work_create", "work_update", "message_send", "message_pending",
            "required_result_save",
        }
        for tool in tools:
            if tool.name in {"work_get", "work_resolve_reference", "work_structure", "work_history",
                             "work_attachments", "work_event"}:
                assert_public(tool.model_dump(mode="json"))
            if tool.name == "work_attachments":
                assert tool.input_schema["properties"]["observed_revision"]["minLength"] == 1
            assert tool.input_schema.get("additionalProperties") is False
            assert not {"principal", "role", "grant_id", "issuer", "allowed_operations"}.intersection(
                tool.input_schema.get("properties", {}))
        required_result = next(tool for tool in tools if tool.name == "required_result_save")
        assert "operation_id" not in required_result.input_schema["properties"]
        update = next(tool for tool in tools if tool.name == "work_update")
        patch = update.input_schema["$defs"]["ScalarPatch"]
        assert {"priority", "work_type", "review_next_action"} <= patch["properties"].keys() and "gid" not in str(patch).lower()
        introspection = (await client.call_tool("grant_get", {"api_version": "1"})).structured_content
        assert introspection["principal"] == PRINCIPAL.model_dump(mode="json")
        got = (await client.call_tool("work_get", {"api_version": "1"})).structured_content
        assert got["item"]["id"] == str(ACTIVE)
        resolved = await client.call_tool(
            "work_resolve_reference", {"api_version": "1", "reference": "123"}
        )
        assert_public(resolved.model_dump(mode="json"))
        assert resolved.structured_content["item"]["id"] == str(ACTIVE)
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
    assert found.structured_content["items"][0]["context"]["assignee"] == "Ada"
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
