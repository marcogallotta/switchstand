import logging
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from mcp import Client, StdioServerParameters

from switchstand.contracts import AppendResult, Routing, SuggestionResult, WorkItem, WorkResult
from switchstand.mcp import _protect_provider_logs, build_server, server_from_env

ID = UUID("00000000-0000-0000-0000-000000000001")
REFERENCE_ID = UUID("00000000-0000-0000-0000-000000000002")

def item(notes: str = "before", work_id: UUID = ID) -> WorkItem:
    return WorkItem(id=work_id, title="bounded", notes=notes, completed=False,
                    revision="r1", routing=Routing(priority="P0"))

class FakeService:
    async def get(self, request):
        return WorkResult(status="ok", item=item(work_id=request.work_id))
    async def update(self, request):
        return WorkResult(status="stale", item=item(request.patch.notes))
    async def append(self, request):
        return AppendResult(status="ok")
    async def suggest_next(self):
        return SuggestionResult(status="none")

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
            "work_get", "work_update", "work_append", "work_suggest_next"}
        assert all(tool.input_schema.get("additionalProperties") is False and
                   tool.output_schema.get("additionalProperties") is False for tool in tools)
        get_tool = next(tool for tool in tools if tool.name == "work_get")
        assert "work_id" not in get_tool.input_schema["required"]
        assert str(REFERENCE_ID) in (get_tool.description or "")
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
        base = {"api_version": "1", "work_id": str(ID)}
        updated = await client.call_tool(
            "work_update", base | {"observed_revision": "r1", "patch": {"notes": "after"}}
        )
        assert updated.structured_content == WorkResult(status="stale", item=item("after")).model_dump(mode="json")
        appended = await client.call_tool("work_append", base | {"text": "history"})
        assert appended.structured_content == {"status": "ok"}
        suggested = await client.call_tool("work_suggest_next", {"api_version": "1"})
        assert suggested.structured_content == {"status": "none", "item": None}
        rejected = await client.call_tool("work_get", base | {"extra": "secret"})
        assert rejected.is_error

if __name__ == "__main__":
    build_server(FakeService(), ID, (REFERENCE_ID,)).run()
