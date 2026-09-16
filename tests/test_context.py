import os
import stat
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from mcp import Client, StdioServerParameters

from switchstand import context
from switchstand.launch import Authority
from switchstand.mcp import build_context_server

ACTIVE = UUID("00000000-0000-0000-0000-000000000001")


class FakeService:
    async def get(self, request):
        return {"work_id": request.work_id, "related": request.include_related}


def executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_context_server_exposes_only_active_work_get():
    server = build_context_server(FakeService(), ACTIVE)
    assert set(server._tool_manager._tools) == {"work_get"}
    schema = server._tool_manager.get_tool("work_get").parameters
    assert set(schema["properties"]) == {"api_version", "include_related"}
    assert "work_id" not in schema["properties"]


async def test_context_server_real_stdio_exposes_only_work_get():
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__))],
        env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        assert [tool.name for tool in tools] == ["work_get"]
        assert "work_id" not in tools[0].input_schema["properties"]


def test_context_provisions_before_codex_without_provider_token(monkeypatch, tmp_path):
    events = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASANA_TOKEN", "host-secret")
    monkeypatch.setenv("ACTIVE_WORK_ID", "stale")
    monkeypatch.setenv("REFERENCE_WORK_IDS", "stale")

    def fake_provision(repo, active, references, env):
        events.append(("provision", repo, active, references, dict(env)))
        return Authority(ACTIVE, ())

    def fake_exec(file, command, env):
        events.append(("codex", file, command, dict(env)))
        raise RuntimeError("stop after readback")

    monkeypatch.setattr(context, "provision", fake_provision)
    monkeypatch.setattr(context.os, "execvpe", fake_exec)

    with pytest.raises(RuntimeError, match="readback"):
        context.run("1218242783900077")

    assert [event[0] for event in events] == ["provision", "codex"]
    provision_env = events[0][4]
    codex_env = events[1][3]
    assert "ASANA_TOKEN" not in provision_env
    assert "ASANA_TOKEN" not in codex_env
    assert "REFERENCE_WORK_IDS" not in codex_env
    assert codex_env["ACTIVE_WORK_ID"] == str(ACTIVE)
    assert codex_env["SWITCHSTAND_MANAGED"] == "1"
    command = events[1][2]
    assert 'mcp_servers.switchstand.enabled_tools=["work_get"]' in command
    assert "mcp_servers.switchstand_development.enabled=false" in command


def test_context_script_selects_repository_python(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    common = tmp_path / "shared" / ".git"
    python = tmp_path / "shared" / ".venv" / "bin" / "python"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    common.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    fake_bin.mkdir()
    source = Path(__file__).parents[1] / "scripts" / "switchstand-context"
    launcher = scripts / "switchstand-context"
    launcher.write_bytes(source.read_bytes())
    launcher.chmod(0o755)
    executable(fake_bin / "git", f"#!/bin/sh\necho '{common}'\n")
    executable(
        python,
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$RESULT\"\n",
    )
    result_file = tmp_path / "result"
    result = subprocess.run(
        [launcher, "--active", "123"],
        env=os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}", "RESULT": str(result_file)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result_file.read_text().splitlines() == [
        "-m", "switchstand.context", "--active", "123"
    ]


if __name__ == "__main__":
    build_context_server(FakeService(), ACTIVE).run()
