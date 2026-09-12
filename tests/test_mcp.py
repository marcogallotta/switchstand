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
    Routing,
    SourceStoriesResult,
    SourceStory,
    SourceStoryResult,
    SourceTask,
    SourceTaskResult,
    WorkItem,
    WorkResult,
)
from switchstand.mcp import _protect_provider_logs, build_server, server_from_env

ID = UUID("00000000-0000-0000-0000-000000000001")
REFERENCE_ID = UUID("00000000-0000-0000-0000-000000000002")
TASK_GID = "121"
STORY_GID = "456"


def item(notes: str = "before", work_id: UUID = ID) -> WorkItem:
    return WorkItem(id=work_id, title="bounded", notes=notes, completed=False,
                    revision="r1", routing=Routing(priority="P0"))


class FakeService:
    async def get(self, request):
        return WorkResult(status="ok", item=item(work_id=request.work_id))

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


def test_provider_request_logs_are_suppressed(caplog):
    _protect_provider_logs()
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
            "work_get", "source_task", "source_stories", "source_story", "work_append",
        }
        config = tomllib.loads((Path(__file__).parents[1] / ".codex/config.toml").read_text())
        assert set(config["mcp_servers"]["switchstand"]["enabled_tools"]) == {
            tool.name for tool in tools
        }
        assert all(tool.input_schema.get("additionalProperties") is False and
                   tool.output_schema.get("additionalProperties") is False for tool in tools)
        get_tool = next(tool for tool in tools if tool.name == "work_get")
        assert "work_id" not in get_tool.input_schema["required"]
        assert str(REFERENCE_ID) in (get_tool.description or "")
        source_tool = next(tool for tool in tools if tool.name == "source_task")
        assert "task_gid" in source_tool.input_schema["required"]
        assert "work_id" not in source_tool.input_schema.get("properties", {})
        assert not (await client.list_resources()).resources
        assert not (await client.list_prompts()).prompts

        got = await client.call_tool("work_get", {"api_version": "1"})
        assert got.structured_content == WorkResult(status="ok", item=item()).model_dump(mode="json")
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

        base = {"api_version": "1", "work_id": str(ID)}
        appended = await client.call_tool("work_append", base | {"text": "history"})
        assert appended.structured_content == {
            "status": "ok", "task_gid": TASK_GID, "story_gid": STORY_GID
        }
        rejected = await client.call_tool("work_get", base | {"extra": "secret"})
        assert rejected.is_error


if __name__ == "__main__":
    build_server(FakeService(), ID, (REFERENCE_ID,)).run()
