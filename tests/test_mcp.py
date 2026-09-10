import logging
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from mcp import Client, StdioServerParameters

from switchstand.contracts import AppendResult, Routing, SuggestionResult, WorkItem, WorkResult
from switchstand.mcp import _protect_provider_logs, build_server

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


async def test_managed_controller_blocks_candidate_mismatch_before_provider_setup(
    monkeypatch, tmp_path
):
    path = tmp_path / "receipt.json"
    prepared = PreparedLaunch(
        authority=LaunchAuthority(active_work_id=ID),
        verification=LaunchVerification(
            active=WorkFingerprint(id=ID, title="bounded", revision="r1")
        ),
    )
    create_receipt(path, RunReceipt(
        run_ref=uuid4(), prepared=prepared, base_sha="a" * 40,
        candidate_sha="b" * 40, isolated_root="/repo", phase="BOUND",
    ))
    monkeypatch.setenv("SWITCHSTAND_RUN_RECEIPT", str(path))
    monkeypatch.setenv("SWITCHSTAND_CANDIDATE_SHA", "c" * 40)
    with pytest.raises(RuntimeError, match="rebuild the exact candidate"):
        await controller_from_env()
    blocked = read_receipt(path)
    assert blocked.phase == "BLOCKED"
    assert blocked.outcome == "candidate identity mismatch"

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
        update_tool = next(tool for tool in tools if tool.name == "work_update")
        append_tool = next(tool for tool in tools if tool.name == "work_append")
        assert get_tool.annotations is not None and get_tool.annotations.read_only_hint is True
        assert update_tool.annotations is not None
        assert update_tool.annotations.read_only_hint is False
        assert update_tool.annotations.destructive_hint is True
        assert append_tool.annotations is not None
        assert append_tool.annotations.read_only_hint is False
        assert append_tool.annotations.destructive_hint is False
        assert "work_id" not in get_tool.input_schema["required"]
        assert str(REFERENCE_ID) not in (get_tool.description or "")
        assert "authoritative" in (get_tool.description or "")
        assert not (await client.list_resources()).resources
        assert not (await client.list_prompts()).prompts
        got = await client.call_tool("work_get", {"api_version": "1"})
        launch = LaunchBinding(active_work_id=ID, reference_work_ids=(REFERENCE_ID,),
                               reference_count=1)
        assert got.structured_content == WorkResult(
            status="ok", item=item(), launch=launch
        ).model_dump(mode="json")
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
