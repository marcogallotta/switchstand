import logging
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from chatgpt_fixture import assert_public, read_chain
from mcp import Client, StdioServerParameters

from switchstand.contracts import (
    AppendResult,
    RelatedCandidate,
    RelatedLookup,
    Routing,
    SourceStoriesResult,
    SourceStory,
    SourceStoryResult,
    SourceTask,
    SourceTaskResult,
    WorkAttachment,
    WorkAttachmentsResult,
    WorkItem,
    WorkResult,
)
from switchstand.grants import GrantedWorkResult
from switchstand.mcp import (
    build_server,
    controller_from_env,
    project_work,
    protect_provider_logs,
    server_from_env,
)

ID = UUID("00000000-0000-0000-0000-000000000001")
REFERENCE_ID = UUID("00000000-0000-0000-0000-000000000002")
TASK_GID = "121"
STORY_GID = "456"


def item(notes: str = "before", work_id: UUID = ID) -> WorkItem:
    return WorkItem(id=work_id, title="bounded", notes=notes, completed=False,
                    revision="r1", routing=Routing(priority="P0"))


class FakeService:
    async def get(self, request):
        related = (RelatedLookup(status="CANDIDATES", work_task_gid=TASK_GID,
                                 observed_revision="r1",
                                 candidates=(RelatedCandidate(task_gid="789", title="Review",
                                             revision="r1", parent_gid=TASK_GID),))
                   if request.include_related else None)
        return WorkResult(status="ok", item=item(work_id=request.work_id), related=related)

    async def attachments(self, request):
        return WorkAttachmentsResult(
            status="ok", work_id=request.work_id, revision=request.observed_revision,
            attachments=(WorkAttachment(name="brief.txt"),), next_cursor="next",
        )

    async def source_task(self, request):
        return SourceTaskResult(
            status="ok",
            item=SourceTask(
                task_gid=request.task_gid, title="source", notes="notes",
                completed=False, revision="r1",
            ),
        )

    async def source_stories(self, request):
        return SourceStoriesResult(
            status="ok", task_gid=request.task_gid, revision=request.observed_revision,
            stories=(SourceStory(
                story_gid=STORY_GID, task_gid=request.task_gid, subtype="comment_added",
                text="history", created_at="2026-09-12T00:00:00Z", created_by="Marco",
            ),),
        )

    async def source_story(self, request):
        return SourceStoryResult(
            status="ok", task_gid=request.task_gid, revision=request.observed_revision,
            item=SourceStory(
                story_gid=request.story_gid, task_gid=request.task_gid,
                subtype="comment_added", text="history",
                created_at="2026-09-12T00:00:00Z", created_by="Marco",
            ),
        )

    async def append(self, request):
        return AppendResult(status="ok", task_gid=TASK_GID, story_gid=STORY_GID)


@pytest.mark.parametrize("kind", [WorkResult, GrantedWorkResult])
@pytest.mark.parametrize("status", ["ok", "stale", "denied", "unknown", "provider_error"])
@pytest.mark.parametrize("include_related", [False, True])
def test_public_projection_allowlist(kind, status, include_related):
    import json

    from switchstand.contracts import GroupedCandidate, GroupedLookup, WorkSource
    from switchstand.mcp import PublicWorkResult, project_work

    value = item().model_copy(update={"source": WorkSource(provider="secret-provider", task_gid="raw-task")})
    fields = {"status": status, "item": value if status in {"ok", "stale"} else None}
    if kind is GrantedWorkResult and status == "denied":
        from switchstand.chatgpt import ChatGPTService
        fields["guard"] = ChatGPTService.denied("work_get")
    if status == "ok":
        fields["related"] = RelatedLookup(status="CANDIDATES", work_task_gid="raw-work",
            observed_revision="r1", reason="raw-reason", candidates=(RelatedCandidate(
                task_gid="raw-child", title="Review", revision="r1", parent_gid="raw-parent",
                work_type_option_gid="raw-option"),))
        if kind is WorkResult:
            fields["grouped"] = GroupedLookup(status="CANDIDATES", root_task_gid="raw-root",
                candidates=(GroupedCandidate(task_gid="raw-group", title="Group", revision="r1",
                    root_work_gid="raw-root", source="asana_root_work_gid_search_exact_get"),))
    output = project_work(kind(**fields), include_related).model_dump(mode="json")
    serialized = json.dumps(output) + json.dumps(PublicWorkResult.model_json_schema())
    for forbidden in ("raw-", "secret-provider", "asana", "task_gid", "parent_gid", "source", "receipt"):
        assert forbidden not in serialized
    assert output["status"] == status
    if status in {"ok", "stale"}:
        assert output["item"] == value.model_dump(mode="json", exclude={"source"})
    else:
        assert all(v is None for k, v in output.items() if k not in {"status", "guard"})
    if include_related and status == "ok":
        assert output["related"] == {"status": "CANDIDATES", "observed_revision": "r1",
            "candidates": [{"title": "Review", "revision": "r1"}], "complete": False}
    else:
        assert output["related"] is None and output["grouped"] is None


def test_provider_request_logs_are_suppressed(caplog):
    protect_provider_logs()
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info("GET https://provider.invalid/tasks/raw-provider-id")
        logging.getLogger("httpcore.connection").warning("raw-provider-id")
    assert "raw-provider-id" not in caplog.text


def test_unbound_environment_initializes_with_no_work_tools(monkeypatch):
    monkeypatch.delenv("SWITCHSTAND_MANAGED", raising=False)
    monkeypatch.delenv("ACTIVE_WORK_ID", raising=False)
    monkeypatch.setenv("ASANA_TOKEN", "must-not-create-a-provider")
    assert not server_from_env()._tool_manager._tools


def test_managed_environment_without_authority_fails(monkeypatch):
    monkeypatch.setenv("SWITCHSTAND_MANAGED", "1")
    monkeypatch.delenv("ACTIVE_WORK_ID", raising=False)
    with pytest.raises(KeyError, match="ACTIVE_WORK_ID"):
        server_from_env()


def test_controller_rejects_invalid_trusted_test_project(monkeypatch):
    monkeypatch.setenv("ACTIVE_WORK_ID", str(ID))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/unused")
    monkeypatch.setenv("ASANA_TOKEN", "unused")
    monkeypatch.setenv("SWITCHSTAND_TEST_PROJECT_GID", "invalid")
    with pytest.raises(ValueError, match="invalid test project GID"):
        controller_from_env()


def test_managed_controller_script_without_authority_fails():
    script = Path(__file__).parents[1] / "scripts" / "switchstand-controller-mcp"
    environment = os.environ | {"SWITCHSTAND_MANAGED": "1"}
    environment.pop("ACTIVE_WORK_ID", None)
    result = subprocess.run(
        [script], env=environment, text=True, capture_output=True, check=False
    )
    assert result.returncode != 0
    assert "managed controller requires ACTIVE_WORK_ID" in result.stderr


@pytest.mark.skipif(
    Path("/.dockerenv").exists(),
    reason="real-host Stage-0 environment is not mounted into the quality container",
)
async def test_unbound_launcher_completes_stdio_handshake():
    script = Path(__file__).parents[1] / "scripts" / "switchstand-controller-mcp"
    server = StdioServerParameters(
        command=str(script), env={"HOME": str(Path.home()), "PATH": os.environ["PATH"]}
    )
    async with Client(server) as client:
        assert not (await client.list_tools()).tools


async def test_real_stdio_handshake_exposes_exact_surface():
    server = StdioServerParameters(
        command=sys.executable, args=[str(Path(__file__)), "serve"],
        env={"PYTHONPATH": str(Path.cwd() / "src")},
    )
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {
            "work_get", "work_attachments", "source_task", "source_stories", "source_story",
            "work_history", "work_event", "work_append",
        }
        config = tomllib.loads((Path(__file__).parents[1] / ".codex/config.toml").read_text())
        assert set(config["mcp_servers"]["switchstand"]["enabled_tools"]) == {
            tool.name for tool in tools
        }
        assert all(tool.input_schema.get("additionalProperties") is False and
                   tool.output_schema.get("additionalProperties") is False for tool in tools)
        get_tool = next(tool for tool in tools if tool.name == "work_get")
        assert "work_id" not in get_tool.input_schema["required"]
        assert "include_related" in get_tool.input_schema["properties"]
        assert str(REFERENCE_ID) in (get_tool.description or "")
        source_tool = next(tool for tool in tools if tool.name == "source_task")
        assert "task_gid" in source_tool.input_schema["required"]
        assert "work_id" not in source_tool.input_schema.get("properties", {})
        assert not (await client.list_resources()).resources
        assert not (await client.list_prompts()).prompts

        got = await client.call_tool("work_get", {"api_version": "1"})
        assert got.structured_content == project_work(WorkResult(status="ok", item=item()), False).model_dump(mode="json")
        related = await client.call_tool("work_get", {"api_version": "1", "include_related": True})
        assert related.structured_content["related"]["candidates"] == [{"title": "Review", "revision": "r1"}]
        reference = await client.call_tool(
            "work_get", {"api_version": "1", "work_id": str(REFERENCE_ID)}
        )
        assert reference.structured_content == project_work(WorkResult(
            status="ok", item=item(work_id=REFERENCE_ID)
        ), False).model_dump(mode="json")

        source = await client.call_tool(
            "source_task", {"api_version": "1", "task_gid": TASK_GID}
        )
        assert source.structured_content == SourceTaskResult(
            status="ok",
            item=SourceTask(
                task_gid=TASK_GID, title="source", notes="notes",
                completed=False, revision="r1",
            ),
        ).model_dump(mode="json")
        stories = await client.call_tool(
            "source_stories",
            {"api_version": "1", "task_gid": TASK_GID, "observed_revision": "r1"},
        )
        assert stories.structured_content["status"] == "ok"
        assert stories.structured_content["stories"][0]["story_gid"] == STORY_GID
        story = await client.call_tool(
            "source_story",
            {
                "api_version": "1", "task_gid": TASK_GID,
                "story_gid": STORY_GID, "observed_revision": "r1",
            },
        )
        assert story.structured_content["status"] == "ok"
        assert story.structured_content["item"]["task_gid"] == TASK_GID

        base = {"api_version": "1", "work_id": str(ID)}
        appended = await client.call_tool("work_append", base | {"text": "history"})
        assert appended.structured_content == {
            "status": "ok", "task_gid": TASK_GID, "story_gid": STORY_GID
        }
        rejected = await client.call_tool("work_get", base | {"extra": "secret"})
        assert rejected.is_error


async def test_managed_attachment_tool_uses_controller_and_asana_boundary():
    from test_source_history import FakeState

    from switchstand.contracts import LaunchAuthority
    from switchstand.core import Controller
    from switchstand.provider import PROJECT, AsanaProvider

    def respond(request):
        if request.url.path.endswith("/attachments"):
            return httpx.Response(200, json={
                "data": [{"gid": "hidden", "name": "brief.txt",
                          "parent": {"gid": "1218431511675555"},
                          "download_url": "https://secret.invalid"}],
                "next_page": {"offset": "next"},
            })
        return httpx.Response(200, json={"data": {
            "name": "Task", "notes": "Notes", "completed": False, "modified_at": "r1",
            "memberships": [{"project": {"gid": PROJECT}}], "parent": None,
            "custom_fields": [],
        }})

    async with httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", transport=httpx.MockTransport(respond)
    ) as http:
        service = Controller(
            LaunchAuthority(active_work_id=ID), FakeState(), {"asana": AsanaProvider(http)}
        )
        async with Client(build_server(service, ID)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            schema = tools["work_attachments"].input_schema
            assert "work_id" not in schema["required"]
            assert schema["properties"]["limit"]["maximum"] == 100
            assert schema["properties"]["cursor"]["anyOf"][0]["maxLength"] == 1024
            revision = (await client.call_tool(
                "work_get", {"api_version": "1"}
            )).structured_content["item"]["revision"]
            result = await client.call_tool(
                "work_attachments", {"api_version": "1", "observed_revision": revision}
            )
            assert result.structured_content == {
                "status": "ok", "work_id": str(ID), "revision": "r1",
                "attachments": [{"name": "brief.txt"}], "next_cursor": "next",
            }
            assert "provider" not in str(result.structured_content)
            rejected = await client.call_tool(
                "work_attachments", {"api_version": "1", "observed_revision": "r1",
                                     "extra": "secret"}
            )
            assert rejected.is_error


async def test_managed_controller_stdio_read_chain():
    server = StdioServerParameters(command=sys.executable, args=[__file__, "managed"],
                                  env={"PYTHONPATH": str(Path.cwd() / "src")})
    async with Client(server) as client:
        for tool in (await client.list_tools()).tools:
            if tool.name in {"work_get", "work_history", "work_event"}:
                assert_public(tool.model_dump(mode="json"))
        for target in (ID, REFERENCE_ID):
            event_id = await read_chain(client, target)
        for tool, extra in (("work_get", {}), ("work_history", {"observed_revision": "r1"}),
                            ("work_event", {"observed_revision": "r1", "event_id": event_id})):
            denied = await client.call_tool(tool, {"api_version": "1", "work_id": str(uuid4()), **extra})
            assert_public(denied.model_dump(mode="json"))
            assert denied.structured_content["status"] == "denied"


async def test_managed_read_denial_precedes_provider_and_failure_projection(monkeypatch):
    from unittest.mock import AsyncMock

    from test_source_history import FakeProvider, FakeState

    from switchstand.contracts import LaunchAuthority
    from switchstand.core import Controller, ProviderError, UnknownEffect

    provider, state = FakeProvider(), FakeState()
    server = build_server(Controller(LaunchAuthority(active_work_id=ID), state, {"asana": provider}), ID)
    binding = await state.bind_event(ID, "asana", "1218431511675555", "1218431592688855")
    for failure, status in ((AssertionError("unauthorized access"), "denied"),
                            (UnknownEffect("raw-provider-error"), "unknown"),
                            (ProviderError("raw-provider-error"), "provider_error")):
        target = uuid4() if status == "denied" else ID
        with monkeypatch.context() as patch:
            for method in ("get", "source_task", "source_stories", "source_story"):
                patch.setattr(provider, method, AsyncMock(side_effect=failure))
            for tool, extra in (("work_get", {}), ("work_history", {"observed_revision": "r1"}),
                                ("work_event", {"observed_revision": "r1", "event_id": str(binding.id)})):
                result = await server.call_tool(tool, {"api_version": "1", "work_id": str(target), **extra})
                assert_public(result.model_dump(mode="json"))
                assert "raw-provider-error" not in str(result)
                assert result.structured_content["status"] == status


if __name__ == "__main__":
    if sys.argv[-1] == "managed":
        from test_source_history import FakeProvider, FakeState

        from switchstand.contracts import LaunchAuthority
        from switchstand.core import Controller

        build_server(Controller(LaunchAuthority(active_work_id=ID, reference_work_ids=(REFERENCE_ID,)),
                                FakeState(), {"asana": FakeProvider()}), ID, (REFERENCE_ID,)).run()
    else:
        build_server(FakeService(), ID, (REFERENCE_ID,)).run()
