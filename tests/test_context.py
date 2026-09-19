import json
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


def hook(repo: Path, command: str, environment: dict[str, str]) -> dict:
    result = subprocess.run(
        [str(Path(__file__).parents[1] / "scripts/codex-hook")],
        input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                          "tool_input": {"command": command}, "cwd": str(repo)}),
        text=True, capture_output=True, check=True, env=environment,
    )
    return json.loads(result.stdout) if result.stdout else {}


def test_context_server_exposes_only_active_work_get():
    server = build_context_server(FakeService(), ACTIVE)
    assert set(server._tool_manager._tools) == {"work_get"}
    schema = server._tool_manager.get_tool("work_get").parameters
    assert set(schema["properties"]) == {"api_version", "include_related"}
    assert "work_id" not in schema["properties"]
    annotations = server._tool_manager.get_tool("work_get").annotations
    assert annotations is not None
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False


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

    def fake_supervise(command, env, temporary_parent):
        assert temporary_parent == tmp_path / "writer/.git"
        events.append(("codex", "codex", command, dict(env)))
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

    def fake_preflight(repo, env):
        events.append(("preflight", repo))
        return str(tmp_path / "pinned-uv")

    monkeypatch.setattr(context, "prepared_check_environment", fake_preflight)
    monkeypatch.setattr(context, "provision", fake_provision)
    monkeypatch.setattr(context, "validate_control", lambda repo, env: repo.resolve())
    monkeypatch.setattr(context, "create_writer", fake_create_writer)
    monkeypatch.setattr(
        context, "managed_codex_home", lambda control, writer, active, env: tmp_path / "codex"
    )
    monkeypatch.setattr(context.subprocess, "run", fake_run)
    monkeypatch.setattr(context.shutil, "which", lambda *args, **kwargs: "/bin/true")
    monkeypatch.setattr(context, "supervise", fake_supervise)

    with pytest.raises(RuntimeError, match="readback"):
        context.run("1218242783900077")

    assert [event[0] for event in events] == ["preflight", "writer", "provision", "codex"]
    provision_env = events[2][4]
    codex_env = events[3][3]
    assert codex_env["SWITCHSTAND_CHECK_UV"] == str(tmp_path / "pinned-uv")
    assert "ASANA_TOKEN" not in provision_env
    assert "ASANA_TOKEN" not in codex_env
    assert "REFERENCE_WORK_IDS" not in codex_env
    assert codex_env["ACTIVE_WORK_ID"] == str(ACTIVE)
    assert codex_env["SWITCHSTAND_MANAGED"] == "1"
    command = events[3][2]
    assert command[1:3] == ["-C", str(writer)]
    assert command[3:6] == ["-a", "never", "--dangerously-bypass-hook-trust"]
    assert 'mcp_servers.switchstand.enabled_tools=["work_get"]' in command
    assert 'mcp_servers.switchstand.default_tools_approval_mode="auto"' in command
    assert 'mcp_servers.switchstand.tools.work_get.approval_mode="auto"' in command
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


def test_detached_control_fetches_and_fast_forwards_to_remote_main(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    primary = tmp_path / "primary"
    control = tmp_path / "control"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "clone", str(remote), str(seed))
    git(seed, "switch", "-c", "main")
    git(seed, "config", "user.name", "Switchstand Test")
    git(seed, "config", "user.email", "switchstand-test@example.invalid")
    (seed / "scripts").mkdir()
    executable(seed / "scripts/codex-hook", "#!/bin/sh\nexit 0\n")
    (seed / "tracked.txt").write_text("base\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "base")
    git(seed, "push", "-u", "origin", "main")
    git(tmp_path, "clone", "--branch", "main", str(remote), str(primary))
    base = git(primary, "rev-parse", "HEAD")
    git(primary, "worktree", "add", "--detach", str(control), base)
    (seed / "tracked.txt").write_text("current\n")
    git(seed, "add", "tracked.txt")
    git(seed, "commit", "-m", "current")
    git(seed, "push", "origin", "main")
    current = git(seed, "rev-parse", "HEAD")

    assert context.validate_control(control, dict(os.environ)) == control
    assert git(control, "rev-parse", "HEAD") == current
    assert git(control, "status", "--short") == ""


def test_detached_control_preserves_dirty_and_divergent_state(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    primary = tmp_path / "primary"
    control = tmp_path / "control"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "clone", str(remote), str(seed))
    git(seed, "switch", "-c", "main")
    git(seed, "config", "user.name", "Switchstand Test")
    git(seed, "config", "user.email", "switchstand-test@example.invalid")
    (seed / "scripts").mkdir()
    executable(seed / "scripts/codex-hook", "#!/bin/sh\nexit 0\n")
    (seed / "tracked.txt").write_text("base\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "base")
    git(seed, "push", "-u", "origin", "main")
    git(tmp_path, "clone", "--branch", "main", str(remote), str(primary))
    git(primary, "config", "user.name", "Switchstand Test")
    git(primary, "config", "user.email", "switchstand-test@example.invalid")
    git(primary, "worktree", "add", "--detach", str(control))
    original = git(control, "rev-parse", "HEAD")

    (control / "unfinished.txt").write_text("preserve\n")
    with pytest.raises(ValueError, match="dirty CONTROL; local work is intact"):
        context.validate_control(control, dict(os.environ))
    assert git(control, "rev-parse", "HEAD") == original
    assert (control / "unfinished.txt").read_text() == "preserve\n"

    (control / "unfinished.txt").unlink()
    (control / "local.txt").write_text("local\n")
    git(control, "add", "local.txt")
    git(control, "commit", "-m", "divergent control")
    divergent = git(control, "rev-parse", "HEAD")
    (seed / "remote.txt").write_text("remote\n")
    git(seed, "add", "remote.txt")
    git(seed, "commit", "-m", "advance remote")
    git(seed, "push", "origin", "main")

    with pytest.raises(ValueError, match="not a clean ancestor; local work is intact"):
        context.validate_control(control, dict(os.environ))
    assert git(control, "rev-parse", "HEAD") == divergent


def test_private_writer_is_independent_bound_and_resumes_dirty_progress(tmp_path):
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
    assert git_dir.is_dir() and git(writer, "rev-parse", "--git-common-dir") == ".git"
    assert not (git_dir / "objects/info/alternates").exists()
    assert git(writer, "remote", "get-url", "origin") == "git@github.com:example/switchstand.git"
    assert git(writer, "branch", "--show-current") == "v2-task-1218438438638352"
    assert [git(writer, "config", "--local", key) for key in ("user.name", "user.email")] == [
        "Switchstand Test", "switchstand-test@example.invalid",
    ]
    assert (git_dir / "switchstand-green-sha").read_text() == green + "\n"
    (writer / "unfinished.txt").write_text("preserved\n")
    assert context.validate_writer(control, writer, "1218438438638352", environment) == writer
    assert context.create_writer(control, "1218438438638352", environment) == writer
    assert (writer / "unfinished.txt").read_text() == "preserved\n"
    assert git(writer, "rev-parse", "HEAD") == green
    assert git(control, "status", "--short") == ""


def test_private_writer_resumes_local_commit_when_main_advances(tmp_path):
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
    (writer / "task.txt").write_text("progress\n")
    git(writer, "add", "task.txt")
    git(writer, "commit", "-m", "task progress")
    task_head = git(writer, "rev-parse", "HEAD")
    (control / "main.txt").write_text("advanced\n")
    git(control, "add", "main.txt")
    git(control, "commit", "-m", "advance main")

    assert context.create_writer(control, "1218438438638352", environment) == writer
    assert git(writer, "rev-parse", "HEAD") == task_head
    assert (writer / ".git/switchstand-green-sha").read_text() == green + "\n"


def test_run_reexecutes_updated_control_before_shared_effects(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    old, accepted = "a" * 40, "b" * 40
    heads = iter((old, accepted))
    monkeypatch.setattr(context, "_git", lambda *args, **kwargs: next(heads))
    monkeypatch.setattr(context, "validate_control", lambda repo, env: repo.resolve())

    def forbidden(*args, **kwargs):
        pytest.fail("shared effect ran before accepted launcher re-exec")

    monkeypatch.setattr(context, "prepared_check_environment", forbidden)
    monkeypatch.setattr(context, "create_writer", forbidden)
    monkeypatch.setattr(context, "provision", forbidden)

    class Reexec(Exception):
        pass

    def execv(path, arguments):
        assert path == str(tmp_path / "scripts/switchstand")
        assert arguments == [path, "--active", "1218483858041754"]
        raise Reexec

    monkeypatch.setattr(context.os, "execv", execv)
    with pytest.raises(Reexec):
        context.run("1218483858041754")


def test_clean_private_writer_fast_forwards_to_control(tmp_path):
    control = tmp_path / "control"
    control.mkdir()
    git(control, "init", "-b", "main")
    git(control, "config", "user.name", "Switchstand Test")
    git(control, "config", "user.email", "switchstand-test@example.invalid")
    (control / "tracked.txt").write_text("base\n")
    git(control, "add", "tracked.txt")
    git(control, "commit", "-m", "base")
    git(control, "remote", "add", "origin", "git@github.com:example/switchstand.git")
    environment = os.environ | {"HOME": str(tmp_path)}
    writer = context.create_writer(control, "1218438438638352", environment)

    (control / "tracked.txt").write_text("current\n")
    git(control, "add", "tracked.txt")
    git(control, "commit", "-m", "current")
    current = git(control, "rev-parse", "HEAD")

    assert context.create_writer(control, "1218438438638352", environment) == writer
    assert git(writer, "rev-parse", "HEAD") == current
    assert (writer / ".git/switchstand-green-sha").read_text() == current + "\n"
    assert (writer / "tracked.txt").read_text() == "current\n"


def test_failed_private_writer_creation_leaves_canonical_path_retryable(monkeypatch, tmp_path):
    control = tmp_path / "control"
    control.mkdir()
    git(control, "init", "-b", "main")
    git(control, "config", "user.name", "Switchstand Test")
    git(control, "config", "user.email", "switchstand-test@example.invalid")
    (control / "tracked.txt").write_text("base\n")
    git(control, "add", "tracked.txt")
    git(control, "commit", "-m", "base")
    git(control, "remote", "add", "origin", "git@github.com:example/switchstand.git")
    environment = os.environ | {"HOME": str(tmp_path)}
    bind_identity = context._bind_git_identity

    def fail_identity(*args, **kwargs):
        raise RuntimeError("identity unavailable")

    monkeypatch.setattr(context, "_bind_git_identity", fail_identity)
    with pytest.raises(RuntimeError, match="identity unavailable"):
        context.create_writer(control, "1218438438638352", environment)

    root = context.durable_root(environment)
    assert list(root.iterdir()) == []
    monkeypatch.setattr(context, "_bind_git_identity", bind_identity)
    writer = context.create_writer(control, "1218438438638352", environment)
    assert writer == root / "task-1218438438638352"
    assert context.validate_writer(control, writer, "1218438438638352", environment) == writer


def test_exact_private_task_writer_allows_commit_while_primary_is_denied(tmp_path):
    primary = tmp_path / "primary"
    writer = tmp_path / "writer"
    for repo in (primary, writer):
        repo.mkdir()
        git(repo, "init", "-b", "main")
    task = "1218438438638352"
    (writer / ".git/switchstand-active-task").write_text(task + "\n")
    environment = os.environ | {
        "SWITCHSTAND_TASK_WRITER": str(writer), "SWITCHSTAND_TASK_ID": task,
    }

    assert hook(writer, "git add README.md", environment) == {}
    assert hook(writer, "git commit -m test", environment) == {}
    denied = hook(primary, "git add README.md", environment)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "primary-checkout" in denied["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("origin", ["../repo", "relative", "/tmp/repo", "file:///tmp/repo"])
def test_provider_origin_rejects_local_transports(monkeypatch, tmp_path, origin):
    monkeypatch.setattr(context, "_git", lambda *args, **kwargs: origin)
    with pytest.raises(ValueError, match="external provider"):
        context._provider_origin(tmp_path, dict(os.environ))


def test_durable_writer_root_rejects_symlink_and_permissive_directory(tmp_path):
    root = tmp_path / ".local/state/switchstand/writers"
    root.parent.mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="real user-owned 0700"):
        context.durable_root(os.environ | {"HOME": str(tmp_path), "SWITCHSTAND_CHECK_UV": str(tmp_path / "pinned-uv")})
    root.unlink()
    root.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="real user-owned 0700"):
        context.durable_root(os.environ | {"HOME": str(tmp_path), "SWITCHSTAND_CHECK_UV": str(tmp_path / "pinned-uv")})


def test_managed_codex_home_has_only_control_hook_and_protected_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(context.shutil, "which", lambda *args, **kwargs: sys.executable)
    control = tmp_path / "control"
    writer = tmp_path / "writer"
    (control / "scripts").mkdir(parents=True)
    writer.mkdir()
    executable(control / "scripts/codex-hook", "#!/bin/sh\nexit 0\n")
    auth = tmp_path / ".codex/auth.json"
    auth.parent.mkdir()
    auth.write_text("{}\n")
    auth.chmod(0o600)

    managed = context.managed_codex_home(control, writer, "1218438438638352",
                                         os.environ | {"HOME": str(tmp_path), "SWITCHSTAND_CHECK_UV": str(tmp_path / "pinned-uv")})

    assert (managed / "auth.json").is_symlink()
    assert (managed / "auth.json").resolve() == auth
    hooks = (managed / "hooks.json").read_text()
    assert hooks.count(str(control / "scripts/codex-hook")) == 2
    assert "codex-hook-router" not in hooks
    config = (managed / "config.toml").read_text()
    assert 'approval_policy = "never"' in config
    assert f'[projects."{writer}"]' in config
    assert 'trust_level = "untrusted"' in config
    assert f'"{control}" = "read"' not in config
    assert f'"{tmp_path / "pinned-uv"}" = "read"' in config
    assert f'"{tmp_path / "primary"}" = "read"' not in config
    assert "pyproject.toml" not in config and "uv.lock" not in config
    assert '".git" = "write"' in config
    assert f'"{managed}" = "deny"' in config
    assert f'"{auth}" = "deny"' in config


def test_managed_codex_home_rejects_symlinked_config(monkeypatch, tmp_path):
    monkeypatch.setattr(context.shutil, "which", lambda *args, **kwargs: sys.executable)
    control = tmp_path / "control"
    writer = tmp_path / "writer"
    (control / "scripts").mkdir(parents=True)
    writer.mkdir()
    executable(control / "scripts/codex-hook", "#!/bin/sh\nexit 0\n")
    auth = tmp_path / ".codex/auth.json"
    auth.parent.mkdir()
    auth.write_text("{}\n")
    auth.chmod(0o600)
    managed = tmp_path / ".local/state/switchstand/codex/task-1218438438638352"
    managed.mkdir(parents=True, mode=0o700)
    (tmp_path / ".local/state/switchstand/codex").chmod(0o700)
    victim = tmp_path / "victim"
    victim.write_text("intact\n")
    (managed / "config.toml").symlink_to(victim)

    with pytest.raises(ValueError, match="unsafe"):
        context.managed_codex_home(control, writer, "1218438438638352",
                                   os.environ | {"HOME": str(tmp_path), "SWITCHSTAND_CHECK_UV": str(tmp_path / "pinned-uv")})
    assert victim.read_text() == "intact\n"


if __name__ == "__main__":
    build_context_server(FakeService(), ACTIVE).run()


def test_preflight_from_linked_control_needs_only_pinned_uv(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    git(primary, "init", "-b", "main")
    git(primary, "config", "user.name", "Test")
    git(primary, "config", "user.email", "test@example.invalid")
    (primary / "scripts").mkdir()
    bootstrap = primary / "scripts/bootstrap"
    bootstrap.write_bytes((Path(__file__).parents[1] / "scripts/bootstrap").read_bytes())
    bootstrap.chmod(0o755)
    (primary / "pyproject.toml").write_text("manifest\n")
    (primary / "uv.lock").write_text("lock\n")
    git(primary, "add", "scripts/bootstrap", "pyproject.toml", "uv.lock")
    git(primary, "commit", "-m", "fixture")
    control = tmp_path / "control"
    git(primary, "worktree", "add", "--detach", str(control))
    tools = primary / ".git/switchstand-tools"
    tools.mkdir()
    executable(tools / "uv-0.12.10", "#!/bin/sh\nexit 0\n")
    assert context.prepared_check_environment(control, dict(os.environ)) == str(tools / "uv-0.12.10")
    assert not (primary / ".venv").exists()
    (tools / "uv-0.12.10").unlink()
    with pytest.raises(ValueError, match="pinned uv missing"):
        context.prepared_check_environment(control, dict(os.environ))


def test_failed_preflight_stops_before_provision_or_codex(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(context, "validate_control", lambda repo, env: repo)
    monkeypatch.setattr(context, "_git", lambda *args, **kwargs: "a" * 40)

    def fail(*args):
        raise ValueError("missing environment; run scripts/bootstrap")

    def forbidden(*args):
        pytest.fail("launch continued past failed preflight")

    monkeypatch.setattr(context, "prepared_check_environment", fail)
    monkeypatch.setattr(context, "provision", forbidden)
    monkeypatch.setattr(context, "create_writer", forbidden)
    monkeypatch.setattr(context.os, "execv", forbidden)
    with pytest.raises(ValueError, match="run scripts/bootstrap"):
        context.run("1218483858041754")
