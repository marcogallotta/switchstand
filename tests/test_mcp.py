import logging
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from uuid import UUID

import pytest
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
    WorkEvent,
    WorkEventResult,
    WorkHistoryResult,
    WorkItem,
    WorkResult,
)
from switchstand.mcp import (
    build_server,
    controller_from_env,
    protect_provider_logs,
    server_from_env,
)

ID = UUID("00000000-0000-0000-0000-000000000001")
REFERENCE_ID = UUID("00000000-0000-0000-0000-000000000002")
TASK_GID = "121"
STORY_GID = "456"
EVENT_ID = UUID("11111111-1111-4111-8111-111111111111")


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

    async def history(self, request):
        return WorkHistoryResult(
            status="ok", work_id=request.work_id, revision=request.observed_revision,
            events=(WorkEvent(
                id=EVENT_ID, work_id=request.work_id, subtype="comment_added",
                text="history", created_at="2026-09-12T00:00:00Z", actor="Marco",
            ),),
        )

    async def event(self, request):
        return WorkEventResult(
            status="ok", work_id=request.work_id, revision=request.observed_revision,
            item=WorkEvent(
                id=request.event_id, work_id=request.work_id, subtype="comment_added",
                text="history", created_at="2026-09-12T00:00:00Z", actor="Marco",
            ),
        )

    async def append(self, request):
        return AppendResult(status="ok", task_gid=TASK_GID, story_gid=STORY_GID)


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
            "work_get", "source_task", "source_stories", "source_story",
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
        assert got.structured_content == WorkResult(status="ok", item=item()).model_dump(mode="json")
        related = await client.call_tool("work_get", {"api_version": "1", "include_related": True})
        assert related.structured_content["related"]["candidates"][0]["parent_gid"] == TASK_GID
        reference = await client.call_tool(
            "work_get", {"api_version": "1", "work_id": str(REFERENCE_ID)}
        )
        assert reference.structured_content == WorkResult(
            status="ok", item=item(work_id=REFERENCE_ID)
        ).model_dump(mode="json")

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

        history = await client.call_tool(
            "work_history", {"api_version": "1", "observed_revision": "r1"}
        )
        assert history.structured_content["status"] == "ok"
        assert history.structured_content["events"][0]["id"] == str(EVENT_ID)
        assert "task_gid" not in history.structured_content["events"][0]
        event = await client.call_tool(
            "work_event",
            {
                "api_version": "1", "event_id": str(EVENT_ID),
                "observed_revision": "r1",
            },
        )
        assert event.structured_content["status"] == "ok"
        assert event.structured_content["item"]["work_id"] == str(ID)

        base = {"api_version": "1", "work_id": str(ID)}
        appended = await client.call_tool("work_append", base | {"text": "history"})
        assert appended.structured_content == {
            "status": "ok", "task_gid": TASK_GID, "story_gid": STORY_GID
        }
        rejected = await client.call_tool("work_get", base | {"extra": "secret"})
        assert rejected.is_error


if __name__ == "__main__":
    build_server(FakeService(), ID, (REFERENCE_ID,)).run()
