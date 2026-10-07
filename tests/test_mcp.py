import logging
import os
import subprocess
import sys
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from chatgpt_fixture import assert_public, read_chain
from mcp import Client, StdioServerParameters

from switchstand import context_mcp
from switchstand.contracts import (
    AppendResult,
    LaunchAuthority,
    Routing,
    WorkContext,
    WorkItem,
    WorkResult,
)
from switchstand.grants import GrantedWorkResult
from switchstand.managed_identity import managed_principal
from switchstand.mcp import (
    build_context_server,
    build_server,
    controller_from_env,
    project_work,
    protect_provider_logs,
    server_from_env,
)
from switchstand.messages import RuntimeCurrentness
from switchstand.priority_claim_service import PriorityClaimReadResult, PriorityClaimService
from switchstand.priority_context import PriorityContextResult
from switchstand.task_runs import AgentTaskResult, TaskRunResult, TaskRunResultResult

ID = UUID("00000000-0000-0000-0000-000000000001")
REFERENCE_ID = UUID("00000000-0000-0000-0000-000000000002")
TASK_GID = "121"
STORY_GID = "456"


def item(notes: str = "before", work_id: UUID = ID) -> WorkItem:
    return WorkItem(id=work_id, title="bounded", notes=notes, completed=False,
                    revision="r1", routing=Routing(priority="P0"),
                    context=WorkContext(assignee="Ada", placements=({"area": "Area",
                                                                     "stage": "Doing"},)))


class FakeService:
    async def get(self, request):
        return WorkResult(status="ok", item=item(work_id=request.work_id))

    async def append(self, request):
        return AppendResult(status="ok", task_gid=TASK_GID, story_gid=STORY_GID)


async def test_managed_priority_tools_derive_launch_identity_and_bound_context():
    principal = managed_principal(ID)
    grant = SimpleNamespace(version=7)

    class Grants:
        async def current(self, principal_key):
            assert principal_key == principal.key
            return grant

    class Claims:
        def __init__(self):
            self.calls = []

        async def current(self, kind, subject_id):
            self.calls.append(("current", kind, subject_id))
            return PriorityClaimReadResult(status="ok")

        async def record(self, passed_grants, passed_principal, request):
            self.calls.append(("record", passed_grants, passed_principal, request))
            return PriorityClaimService.guard(request, "denied", "probe")

    class Context:
        def __init__(self):
            self.calls = []

        async def project(self, work_ids):
            self.calls.append(work_ids)
            return PriorityContextResult(status="ok", scope_complete=True)

    grants, claims, context = Grants(), Claims(), Context()
    server = build_server(
        FakeService(), ID, (REFERENCE_ID,),
        grants=grants, principal=principal,
        priority_claims=claims,  # type: ignore[arg-type]
        priority_context=context,  # type: ignore[arg-type]
    )
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert {
            "priority_claim_get", "priority_claim_record", "priority_context_get",
        } <= tools.keys()
        record_schema = tools["priority_claim_record"].input_schema
        assert "work_id" not in record_schema["properties"]
        assert "grant_version" not in record_schema["properties"]

        read = await client.call_tool("priority_claim_get", {"api_version": "1"})
        assert read.structured_content["status"] == "ok"
        operation_id = uuid4()
        recorded = await client.call_tool("priority_claim_record", {
            "api_version": "1", "operation_id": str(operation_id),
            "observed_revision": "r1", "relation_kind": "BAND",
            "band": "NORMAL", "rationale": "inspect after the current blocker",
        })
        assert recorded.structured_content["reason"] == "probe"
        projected = await client.call_tool("priority_context_get", {
            "api_version": "1", "include_references": True,
        })
        assert projected.structured_content["status"] == "ok"

    assert claims.calls[0] == ("current", "WORK", ID)
    request = claims.calls[1][3]
    assert request.work_id == ID
    assert request.grant_version == 7
    assert request.operation_id == operation_id
    assert context.calls == [(ID, REFERENCE_ID)]


async def test_launch_context_server_exposes_same_bound_priority_adapter():
    principal = managed_principal(ID)
    grant = SimpleNamespace(version=7)

    class Grants:
        async def current(self, principal_key):
            assert principal_key == principal.key
            return grant

    class Claims:
        async def current(self, kind, subject_id):
            assert (kind, subject_id) == ("WORK", ID)
            return PriorityClaimReadResult(status="ok")

        async def record(self, passed_grants, passed_principal, request):
            assert passed_grants is grants
            assert passed_principal == principal
            assert request.work_id == ID
            assert request.grant_version == 7
            return PriorityClaimService.guard(request, "denied", "probe")

    class Context:
        async def project(self, work_ids):
            assert work_ids == (ID, REFERENCE_ID)
            return PriorityContextResult(status="ok", scope_complete=True)

    grants = Grants()
    server = build_context_server(
        FakeService(), ID, (REFERENCE_ID,),
        grants=grants, principal=principal,
        priority_claims=Claims(),  # type: ignore[arg-type]
        priority_context=Context(),  # type: ignore[arg-type]
    )
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(tools) == {
            "work_get", "work_history", "priority_claim_get",
            "priority_claim_record", "priority_context_get", "capability_preflight_get",
        }
        schema = tools["priority_claim_record"].input_schema
        assert "work_id" not in schema["properties"]
        assert "grant_version" not in schema["properties"]
        result = await client.call_tool("priority_context_get", {
            "api_version": "1", "include_references": True,
        })
        assert result.structured_content["status"] == "ok"


@pytest.mark.parametrize("enabled", [False, True])
def test_context_mcp_constructs_priority_adapters_only_when_enabled(monkeypatch, enabled):
    engine = object()
    works = object()
    relations = object()
    service = SimpleNamespace(
        authority=LaunchAuthority(active_work_id=ID, reference_work_ids=(REFERENCE_ID,)),
        state=SimpleNamespace(engine=engine),
        work=SimpleNamespace(works=works, relations=relations),
    )
    captured = {}

    class Server:
        def run(self):
            captured["ran"] = True

    def build(passed_service, active, references, **adapters):
        captured.update({
            "service": passed_service, "active": active,
            "references": references, **adapters,
        })
        return Server()

    monkeypatch.setattr(context_mcp, "protect_provider_logs", lambda: None)
    monkeypatch.setattr(context_mcp, "require_current_schema", lambda: None)
    monkeypatch.setattr(context_mcp, "controller_from_env", lambda: service)
    monkeypatch.setattr(context_mcp, "build_context_server", build)
    if enabled:
        monkeypatch.setenv("SWITCHSTAND_PRIORITY_CLAIMS", "1")
    else:
        monkeypatch.delenv("SWITCHSTAND_PRIORITY_CLAIMS", raising=False)

    context_mcp.main()

    assert captured["ran"] is True
    assert captured["service"] is service
    assert captured["active"] == ID
    assert captured["references"] == (REFERENCE_ID,)
    if enabled:
        assert captured["principal"] == managed_principal(ID)
        assert captured["grants"] is not None
        assert captured["priority_claims"] is not None
        assert captured["priority_context"] is not None
    else:
        assert captured["principal"] == managed_principal(ID)
        assert captured["grants"] is not None
        assert captured["priority_claims"] is captured["priority_context"] is None
    assert captured["activation"] is None


@pytest.mark.parametrize("kind", [WorkResult, GrantedWorkResult])
@pytest.mark.parametrize("status", ["ok", "stale", "denied", "unknown", "provider_error"])
def test_public_projection_allowlist(kind, status):
    import json

    from switchstand.contracts import WorkSource
    from switchstand.mcp import PublicWorkResult, project_work

    value = item().model_copy(update={"source": WorkSource(provider="secret-provider", task_gid="raw-task")})
    fields = {"status": status, "item": value if status in {"ok", "stale"} else None}
    if kind is GrantedWorkResult and status == "denied":
        from switchstand.chatgpt import ChatGPTService
        fields["guard"] = ChatGPTService.denied("work_get")
    output = project_work(kind(**fields)).model_dump(mode="json")
    serialized = json.dumps(output) + json.dumps(PublicWorkResult.model_json_schema())
    for forbidden in ("raw-", "secret-provider", "asana", "task_gid", "parent_gid", "source", "receipt"):
        assert forbidden not in serialized
    assert output["status"] == status
    if status in {"ok", "stale"}:
        assert output["item"] == value.model_dump(mode="json", exclude={"source"})
    else:
        assert all(v is None for k, v in output.items() if k not in {"status", "guard"})
    assert "related" not in output
    assert "related" not in kind.model_json_schema()["properties"]


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


def test_managed_controller_needs_no_asana_configuration(monkeypatch):
    monkeypatch.setenv("ACTIVE_WORK_ID", str(ID))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/unused")
    monkeypatch.delenv("ASANA_TOKEN", raising=False)
    monkeypatch.setenv("SWITCHSTAND_TEST_PROJECT_GID", "invalid")
    controller = controller_from_env()

    assert controller.providers == {}
    assert controller.authority.active_work_id == ID


async def test_managed_server_constructs_task_runs_but_defaults_deny(monkeypatch):
    import switchstand.mcp as managed_mcp

    engine, works = object(), object()
    work = SimpleNamespace(works=works, protected_update=None)
    service = SimpleNamespace(
        state=SimpleNamespace(engine=engine),
        work=work,
        authority=LaunchAuthority(active_work_id=ID, reference_work_ids=()),
    )
    constructed = []

    class NoGrantState:
        def __init__(self, value):
            self.engine = value

        @asynccontextmanager
        async def locked(self, _principal_key):
            yield None

    class SpyTaskRunState:
        def __init__(self, value, works):
            constructed.append((value, works))

        async def request(self, *_args):
            raise AssertionError("default-denied request reached persistence")

        async def submit_result(self, *_args):
            raise AssertionError("default-denied result reached persistence")

    monkeypatch.setenv("SWITCHSTAND_MANAGED", "1")
    monkeypatch.setattr(managed_mcp, "controller_from_env", lambda: service)
    monkeypatch.setattr(managed_mcp, "GrantState", NoGrantState)
    monkeypatch.setattr(managed_mcp, "TaskRunState", SpyTaskRunState)

    server = managed_mcp.server_from_env()
    assert constructed == [(engine, works)]
    assert "agent_task_request" in server._tool_manager._tools
    result = await server.call_tool("agent_task_request", {
        "api_version": "1",
        "execution_work_id": str(ID),
        "observed_revision": "r1",
        "task_kind": "INVESTIGATION",
        "objective": "Prove the default grant remains inert.",
        "result_contract": {},
    })
    assert result.structured_content == {
        "status": "denied", "request": None, "reason": "no_current_grant",
    }
    assert "agent_task_result" in server._tool_manager._tools
    result = await server.call_tool("agent_task_result", {
        "api_version": "1",
        "request_id": str(uuid4()),
        "result_id": str(uuid4()),
        "outcome": "complete",
        "summary": "Evidence remains denied by default.",
        "evidence_refs": [],
    })
    assert result.structured_content == {
        "status": "denied", "result": None, "terminal": False,
        "reason": "no_current_grant",
    }
    config = tomllib.loads((Path(__file__).parents[1] / ".codex/config.toml").read_text())
    enabled = config["mcp_servers"]["switchstand_managed"]["enabled_tools"]
    assert "agent_task_request" not in enabled
    assert "agent_task_result" not in enabled


async def test_agent_task_result_passes_runtime_currentness_to_existing_binding():
    principal = managed_principal(ID)
    request_id, result_id = uuid4(), uuid4()
    presented_run, current_run = uuid4(), uuid4()
    runtimes = (
        RuntimeCurrentness(generation=str(presented_run), current_generation=None),
        RuntimeCurrentness(
            generation=str(presented_run), current_generation=str(current_run)
        ),
    )

    grant = SimpleNamespace(
        principal=principal,
        scope="launch",
        authority=LaunchAuthority(active_work_id=ID, reference_work_ids=()),
        operations=frozenset({"agent_task"}),
        current=lambda: True,
        can_write=lambda work_id: work_id == ID,
    )

    class Grants:
        @asynccontextmanager
        async def locked(self, principal_key):
            assert principal_key == principal.key
            yield grant

    class TaskRuns:
        def __init__(self):
            self.calls = []

        async def submit_result(self, *args):
            self.calls.append(args)
            runtime = args[2]
            return TaskRunResultResult(
                status="unknown" if runtime.current_generation is None else "stale",
                result=TaskRunResult(
                    result_id=result_id,
                    request_id=request_id,
                    run_id=presented_run,
                    outcome="complete",
                    summary="Bound execution evidence.",
                    evidence_refs=("commit:abc",),
                ),
                reason=(
                    "runtime_currentness_unavailable"
                    if runtime.current_generation is None else "run_superseded"
                ),
            )

    task_runs = TaskRuns()
    selected = iter(runtimes)
    server = build_server(
        object(), active_work_id=ID, grants=Grants(), principal=principal,
        currentness=lambda: next(selected), task_runs=task_runs,
    )
    schema = server._tool_manager._tools["agent_task_result"].fn_metadata.arg_model.model_json_schema()
    assert set(schema["properties"]) == {
        "api_version", "request_id", "result_id", "outcome", "summary", "evidence_refs",
    }
    payload = {
        "api_version": "1", "request_id": str(request_id), "result_id": str(result_id),
        "outcome": "complete", "summary": "Bound execution evidence.",
        "evidence_refs": ["commit:abc"],
    }

    for runtime, expected_status in zip(runtimes, ("unknown", "stale"), strict=True):
        response = await server.call_tool("agent_task_result", payload)
        assert response.structured_content["status"] == expected_status
        assert response.structured_content["terminal"] is False
        assert response.structured_content["result"]["evidence_refs"] == ["commit:abc"]
        assert task_runs.calls[-1] == (
            request_id,
            result_id,
            runtime,
            AgentTaskResult(
                api_version="1", outcome="complete", summary="Bound execution evidence.",
                evidence_refs=("commit:abc",),
            ),
        )


async def test_agent_task_result_never_persists_without_runtime_identity():
    principal = managed_principal(ID)
    grant = SimpleNamespace(
        principal=principal,
        scope="launch",
        authority=LaunchAuthority(active_work_id=ID, reference_work_ids=()),
        operations=frozenset({"agent_task"}),
        current=lambda: True,
        can_write=lambda work_id: work_id == ID,
    )

    class Grants:
        @asynccontextmanager
        async def locked(self, _principal_key):
            yield grant

    selected_runtime = None
    server = build_server(
        object(), active_work_id=ID, grants=Grants(), principal=principal,
        currentness=lambda: selected_runtime, task_runs=SimpleNamespace(),
    )
    payload = {
        "api_version": "1", "request_id": str(uuid4()), "result_id": str(uuid4()),
        "outcome": "complete", "summary": "No runtime identity.",
    }
    grant.operations = frozenset()
    response = await server.call_tool("agent_task_result", payload)
    assert response.structured_content["status"] == "denied"
    assert response.structured_content["reason"] == "operation_not_granted"

    grant.operations = frozenset({"agent_task"})
    response = await server.call_tool("agent_task_result", payload)
    assert response.structured_content == {
        "status": "unknown", "result": None, "terminal": False,
        "reason": "runtime_currentness_unavailable",
    }


def test_managed_controller_script_without_authority_fails():
    script = Path(__file__).parents[1] / "scripts" / "switchstand-controller-mcp"
    environment = os.environ | {"SWITCHSTAND_MANAGED": "1"}
    environment.pop("ACTIVE_WORK_ID", None)
    result = subprocess.run(
        [script], env=environment, text=True, capture_output=True, check=False
    )
    assert result.returncode != 0
    assert "managed controller requires ACTIVE_WORK_ID" in result.stderr


async def test_unbound_launcher_completes_stdio_handshake(tmp_path: Path):
    script = Path(__file__).parents[1] / "scripts" / "switchstand-controller-mcp"
    fake_bin = tmp_path / "bin"
    python = tmp_path / "primary" / ".venv" / "bin" / "python"
    fake_bin.mkdir()
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\nexec "$REAL_PYTHON" "$@"\n')
    python.chmod(0o755)
    git = fake_bin / "git"
    git.write_text("#!/bin/sh\nprintf '%s\\n' \"$FAKE_GIT_COMMON\"\n")
    git.chmod(0o755)
    server = StdioServerParameters(
        command=str(script),
        env={
            "FAKE_GIT_COMMON": str(tmp_path / "primary" / ".git"),
            "HOME": str(Path.home()),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REAL_PYTHON": sys.executable,
        },
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
            "work_get", "work_history", "work_event", "work_append",
            "capability_preflight_get",
        }
        config = tomllib.loads((Path(__file__).parents[1] / ".codex/config.toml").read_text())
        assert set(config["mcp_servers"]["switchstand_managed"]["enabled_tools"]) == {
            tool.name for tool in tools
        } | {
            "work_update",
            "priority_claim_get", "priority_claim_record", "priority_context_get",
            "message_pending", "message_receive", "message_recover",
            "message_result_send", "message_disposition",
        }
        assert all(tool.input_schema.get("additionalProperties") is False and
                   tool.output_schema.get("additionalProperties") is False for tool in tools)
        get_tool = next(tool for tool in tools if tool.name == "work_get")
        assert "work_id" not in get_tool.input_schema["required"]
        assert "include_related" not in get_tool.input_schema["properties"]
        assert "related" not in get_tool.output_schema["properties"]
        assert "grouped" not in get_tool.output_schema["properties"]
        assert str(REFERENCE_ID) in (get_tool.description or "")
        assert not (await client.list_resources()).resources
        assert not (await client.list_prompts()).prompts

        got = await client.call_tool("work_get", {"api_version": "1"})
        assert got.structured_content == project_work(WorkResult(status="ok", item=item())).model_dump(mode="json")
        assert "grouped" not in got.structured_content
        related = await client.call_tool("work_get", {"api_version": "1", "include_related": True})
        assert related.is_error
        reference = await client.call_tool(
            "work_get", {"api_version": "1", "work_id": str(REFERENCE_ID)}
        )
        assert reference.structured_content == project_work(WorkResult(
            status="ok", item=item(work_id=REFERENCE_ID)
        )).model_dump(mode="json")

        base = {"api_version": "1", "work_id": str(ID)}
        appended = await client.call_tool("work_append", base | {"text": "history"})
        assert appended.structured_content == {
            "status": "ok", "task_gid": TASK_GID, "story_gid": STORY_GID
        }
        rejected = await client.call_tool("work_get", base | {"extra": "secret"})
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
