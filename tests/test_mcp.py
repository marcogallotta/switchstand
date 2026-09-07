import sys
from pathlib import Path
from uuid import UUID

from mcp import Client, StdioServerParameters

from switchstand.contracts import AppendResult, Routing, WorkItem, WorkResult
from switchstand.mcp import build_server

ID = UUID("00000000-0000-0000-0000-000000000001")

def item(notes: str = "before") -> WorkItem:
    return WorkItem(id=ID, title="bounded", notes=notes, completed=False,
                    revision="r1", routing=Routing(priority="P0"))

class FakeService:
    async def get(self, request):
        return WorkResult(status="ok", item=item())
    async def update(self, request):
        return WorkResult(status="stale", item=item(request.patch.notes))
    async def append(self, request):
        return AppendResult(status="ok")

async def test_real_stdio_handshake_exposes_exact_surface():
    server = StdioServerParameters(command=sys.executable, args=[str(Path(__file__)), "serve"])
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {"work_get", "work_update", "work_append"}
        assert all(tool.input_schema.get("additionalProperties") is False and
                   tool.output_schema.get("additionalProperties") is False for tool in tools)
        assert not (await client.list_resources()).resources
        assert not (await client.list_prompts()).prompts
        base = {"api_version": "1", "work_id": str(ID)}
        got = await client.call_tool("work_get", base)
        assert got.structured_content == WorkResult(status="ok", item=item()).model_dump(mode="json")
        updated = await client.call_tool(
            "work_update", base | {"observed_revision": "r1", "patch": {"notes": "after"}}
        )
        assert updated.structured_content == WorkResult(status="stale", item=item("after")).model_dump(mode="json")
        appended = await client.call_tool("work_append", base | {"text": "history"})
        assert appended.structured_content == {"status": "ok"}
        rejected = await client.call_tool("work_get", base | {"extra": "secret"})
        assert rejected.is_error

if __name__ == "__main__":
    build_server(FakeService()).run()
