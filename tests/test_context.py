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


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


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

    writer = tmp_path / "writer"
    writer.mkdir()
    (tmp_path / ".git").mkdir()

    def fake_create_writer(repo, active, env):
        events.append(("writer", repo, active))
        return writer

    def fake_run(command, **kwargs):
        value = "v2-work-1218242783900077\n" if command[-2:] == ["branch", "--show-current"] else str(tmp_path / ".git") + "\n"
        return subprocess.CompletedProcess(command, 0, stdout=value)

    monkeypatch.setattr(context, "provision", fake_provision)
    monkeypatch.setattr(context, "validate_control", lambda repo, env: repo.resolve())
    monkeypatch.setattr(context, "create_writer", fake_create_writer)
    monkeypatch.setattr(
        context, "managed_codex_home", lambda control, writer, active, env: tmp_path / "codex"
    )
    monkeypatch.setattr(context.subprocess, "run", fake_run)
    monkeypatch.setattr(context.os, "execvpe", fake_exec)

    with pytest.raises(RuntimeError, match="readback"):
        context.run("1218242783900077")

    assert [event[0] for event in events] == ["provision", "writer", "codex"]
    provision_env = events[0][4]
    codex_env = events[2][3]
    assert "ASANA_TOKEN" not in provision_env
    assert "ASANA_TOKEN" not in codex_env
    assert "REFERENCE_WORK_IDS" not in codex_env
    assert codex_env["ACTIVE_WORK_ID"] == str(ACTIVE)
    assert codex_env["SWITCHSTAND_MANAGED"] == "1"
    command = events[2][2]
    assert command[1:3] == ["-C", str(writer)]
    assert command[3:6] == ["-a", "never", "--dangerously-bypass-hook-trust"]
    assert 'mcp_servers.switchstand.enabled_tools=["work_get"]' in command
    assert f'mcp_servers.switchstand.command="{tmp_path / "scripts" / "switchstand-context-mcp"}"' in command
    assert str(writer / "scripts" / "switchstand-context-mcp") not in command


def test_switchstand_script_selects_repository_python(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    common = tmp_path / "shared" / ".git"
    python = tmp_path / "shared" / ".venv" / "bin" / "python"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    common.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    fake_bin.mkdir()
    source = Path(__file__).parents[1] / "scripts" / "switchstand"
    launcher = scripts / "switchstand"
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


def test_switchstand_isolated_dispatches_to_control_launcher(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    launcher = scripts / "switchstand"
    launcher.write_bytes((Path(__file__).parents[1] / "scripts" / "switchstand").read_bytes())
    launcher.chmod(0o755)
    executable(
        scripts / "switchstand-start",
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$RESULT\"\n",
    )
    result_file = tmp_path / "result"

    result = subprocess.run(
        [launcher, "--isolated", "--active", "123", "--commit", "a" * 40],
        env=os.environ | {"RESULT": str(result_file)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result_file.read_text().splitlines() == ["--active", "123", "--commit", "a" * 40]


def test_private_writer_is_independent_bound_and_resumes_dirty(tmp_path):
    control = tmp_path / "control"
    control.mkdir()
    git(control, "init", "-b", "main")
    git(control, "config", "user.name", "Switchstand Test")
    git(control, "config", "user.email", "switchstand-test@example.invalid")
    (control / "tracked.txt").write_text("base\n")
    git(control, "add", "tracked.txt")
    git(control, "commit", "-m", "base")
    green = git(control, "rev-parse", "HEAD")
    git(control, "remote", "add", "origin", "git@github.com:example/switchstand.git")
    environment = os.environ | {"HOME": str(tmp_path)}
    writer = context.create_writer(control, "1218438438638352", environment)
    git_dir = writer / ".git"
    assert git_dir.is_dir()
    assert git(writer, "rev-parse", "--git-common-dir") == ".git"
    assert not (git_dir / "objects/info/alternates").exists()
    assert git(writer, "remote", "get-url", "origin") == "git@github.com:example/switchstand.git"
    assert git(writer, "branch", "--show-current") == "v2-task-1218438438638352"
    assert (git_dir / "switchstand-green-sha").read_text() == green + "\n"
    (writer / "unfinished.txt").write_text("preserved\n")
    assert context.validate_writer(control, writer, "1218438438638352", environment) == writer
    assert context.create_writer(control, "1218438438638352", environment) == writer
    assert (writer / "unfinished.txt").read_text() == "preserved\n"
    assert git(control, "status", "--short") == ""


def test_durable_writer_root_rejects_symlink_and_permissive_directory(tmp_path):
    root = tmp_path / ".local/state/switchstand/writers"
    root.parent.mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="real user-owned 0700"):
        context.durable_root(os.environ | {"HOME": str(tmp_path)})
    root.unlink()
    root.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="real user-owned 0700"):
        context.durable_root(os.environ | {"HOME": str(tmp_path)})


def test_managed_codex_home_has_only_control_hook_and_protected_auth(tmp_path):
    control = tmp_path / "control"
    writer = tmp_path / "writer"
    (control / "scripts").mkdir(parents=True)
    writer.mkdir()
    executable(control / "scripts/codex-hook", "#!/bin/sh\nexit 0\n")
    auth = tmp_path / ".codex/auth.json"
    auth.parent.mkdir()
    auth.write_text("{}\n")
    auth.chmod(0o600)

    managed = context.managed_codex_home(
        control, writer, "1218438438638352", os.environ | {"HOME": str(tmp_path)}
    )

    assert (managed / "auth.json").is_symlink()
    assert (managed / "auth.json").resolve() == auth
    hooks = (managed / "hooks.json").read_text()
    assert hooks.count(str(control / "scripts/codex-hook")) == 2
    assert "codex-hook-router" not in hooks
    config = (managed / "config.toml").read_text()
    assert 'approval_policy = "never"' in config
    assert f'[projects."{writer}"]' in config
    assert 'trust_level = "untrusted"' in config
    assert f'"{managed}" = "deny"' in config
    assert f'"{auth}" = "deny"' in config


if __name__ == "__main__":
    build_context_server(FakeService(), ACTIVE).run()
