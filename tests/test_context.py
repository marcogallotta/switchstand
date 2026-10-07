import json
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from uuid import UUID

import pytest
from mcp import Client, StdioServerParameters

from switchstand import context
from switchstand.contracts import (
    Routing,
    WorkContext,
    WorkHistoryResult,
    WorkItem,
    WorkResult,
    WorkSource,
)
from switchstand.launch import Authority
from switchstand.managed_reentry import MANAGED_DEVELOPER_INSTRUCTIONS
from switchstand.mcp import build_context_server

ACTIVE = UUID("00000000-0000-0000-0000-000000000001")


class FakeService:
    async def get(self, request):
        return WorkResult(status="ok", item=WorkItem(id=request.work_id, title="Task", notes="Notes",
            completed=False, revision="r1", routing=Routing(), context=WorkContext(),
            source=WorkSource(provider="asana", task_gid="raw-task")))

    async def history(self, request):
        return WorkHistoryResult(
            status="ok", work_id=request.work_id,
            revision=request.observed_revision, next_cursor=request.cursor,
        )


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


def hook(
    repo: Path, command: str, environment: dict[str, str],
    *, tool: str = "Bash", coordinator_primary: Path | None = None,
    coordinator_writer: Path | None = None,
    agent_id: str | None = None, tool_workdir: Path | None = None,
) -> dict:
    arguments = [str(Path(__file__).parents[1] / "scripts/codex-hook")]
    if coordinator_primary is not None:
        arguments += ["--coordinator-primary", str(coordinator_primary)]
    arguments += ["--coordinator-writer", str(coordinator_writer)] if coordinator_writer else []
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
               "tool_input": {"file_path" if tool == "Edit" else "command": command},
               "cwd": str(repo)}
    if agent_id is not None:
        payload["agent_id"] = agent_id
    if tool_workdir is not None:
        payload["tool_input"]["workdir"] = str(tool_workdir)
    result = subprocess.run(
        arguments,
        input=json.dumps(payload),
        text=True, capture_output=True, check=True, env=environment,
    )
    return json.loads(result.stdout) if result.stdout else {}


def test_delegated_worker_is_read_only_across_hostile_shell_and_edit_forms(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    git(primary, "init", "-b", "main")
    git(primary, "config", "user.name", "Test")
    git(primary, "config", "user.email", "test@example.invalid")
    (primary / "tracked.txt").write_text("base\n")
    git(primary, "add", "tracked.txt")
    git(primary, "commit", "-m", "base")
    root_writer, assigned, foreign = (tmp_path / name for name in ("root", "assigned", "foreign"))
    for path in (root_writer, assigned, foreign):
        git(primary, "worktree", "add", "-b", path.name, str(path))
    common = {
        "environment": dict(os.environ),
        "coordinator_primary": primary,
        "coordinator_writer": root_writer,
        "agent_id": "worker-123",
    }
    protected = (primary, root_writer, foreign)
    before = {
        repo: (git(repo, "status", "--porcelain=v1"), git(repo, "write-tree"),
               (repo / "tracked.txt").read_text())
        for repo in protected
    }
    root_git_dir = git(root_writer, "rev-parse", "--absolute-git-dir")
    foreign_git_dir = git(foreign, "rev-parse", "--absolute-git-dir")
    hostile = (
        f"printf contaminated > {root_writer / 'tracked.txt'}",
        f"sh -c 'printf contaminated > {foreign / 'tracked.txt'}'",
        f"git -C {foreign} add tracked.txt",
        (
            f"printf contaminated > {root_writer / 'tracked.txt'} && "
            f"git --git-dir={root_git_dir} --work-tree={root_writer} add tracked.txt"
        ),
        (
            f"printf contaminated > {foreign / 'tracked.txt'} && "
            f"git --work-tree={foreign} --git-dir={foreign_git_dir} "
            "update-index --add tracked.txt"
        ),
    )
    decisions = []
    for command in hostile:
        denied = hook(assigned, command, **common)
        decisions.append(denied)
        if not denied:
            subprocess.run(["bash", "-lc", command], cwd=assigned, check=True)

    patch = "*** Begin Patch\n*** Add File: worker-probe\n+x\n*** End Patch"
    for tool, value in (("apply_patch", patch), ("Edit", str(assigned / "worker-probe"))):
        denied = hook(assigned, value, tool=tool, **common)
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "worker-read-only" in denied["hookSpecificOutput"]["permissionDecisionReason"]

    after = {
        repo: (git(repo, "status", "--porcelain=v1"), git(repo, "write-tree"),
               (repo / "tracked.txt").read_text())
        for repo in protected
    }
    assert all(
        decision["hookSpecificOutput"]["permissionDecision"] == "deny"
        and "worker-read-only" in decision["hookSpecificOutput"]["permissionDecisionReason"]
        for decision in decisions
    )
    assert after == before


def test_root_tools_are_not_misclassified_as_delegated_worker(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    git(primary, "init", "-b", "main")
    git(primary, "config", "user.name", "Test")
    git(primary, "config", "user.email", "test@example.invalid")
    (primary / "tracked.txt").write_text("base\n")
    git(primary, "add", "tracked.txt")
    git(primary, "commit", "-m", "base")
    writer = tmp_path / "writer"
    git(primary, "worktree", "add", "--detach", str(writer))
    common = {
        "environment": dict(os.environ),
        "coordinator_primary": primary,
        "coordinator_writer": writer,
    }
    assert hook(writer, "git status --short", **common) == {}
    assert hook(writer, "*** Begin Patch\n*** Add File: probe\n+x\n*** End Patch",
                tool="apply_patch", **common) == {}


def test_context_mcp_wrapper_forwards_priority_flag_to_container(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "switchstand-context-mcp"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "docker-args"
    executable(
        fake_bin / "docker",
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CAPTURE\"\n",
    )
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ACTIVE_WORK_ID": str(ACTIVE),
        "SWITCHSTAND_MANAGED": "1",
        "SWITCHSTAND_PRIORITY_CLAIMS": "1",
        "CAPTURE": str(capture),
    }

    result = subprocess.run([script], env=environment, text=True, capture_output=True, check=False)

    assert result.returncode == 0
    arguments = capture.read_text().splitlines()
    assert ["-e", "SWITCHSTAND_PRIORITY_CLAIMS"] == arguments[
        arguments.index("-e"):arguments.index("-e") + 2
    ]
    assert environment["SWITCHSTAND_PRIORITY_CLAIMS"] == "1"


def test_context_server_exposes_only_bound_read_context():
    server = build_context_server(FakeService(), ACTIVE)
    assert set(server._tool_manager._tools) == {"work_get", "work_history"}
    schema = server._tool_manager.get_tool("work_get").parameters
    assert set(schema["properties"]) == {"api_version"}
    assert "work_id" not in schema["properties"]
    history = server._tool_manager.get_tool("work_history")
    assert set(history.parameters["properties"]) == {
        "api_version", "observed_revision", "cursor", "limit",
    }
    assert "work_id" not in history.parameters["properties"]
    for name in ("work_get", "work_history"):
        annotations = server._tool_manager.get_tool(name).annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is False


async def test_context_server_real_stdio_exposes_only_bound_read_context():
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__))],
        env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        assert [tool.name for tool in tools] == ["work_get", "work_history"]
        assert all("work_id" not in tool.input_schema["properties"] for tool in tools)
        got = await client.call_tool("work_get", {"api_version": "1"})
        assert got.structured_content["item"]["id"] == str(ACTIVE)
        assert "raw-task" not in str(got) and "asana" not in str(got)
        rejected = await client.call_tool(
            "work_get", {"api_version": "1", "include_related": True}
        )
        assert rejected.is_error
        history = await client.call_tool(
            "work_history", {"api_version": "1", "observed_revision": "r1"}
        )
        assert history.structured_content == WorkHistoryResult(
            status="ok", work_id=ACTIVE, revision="r1"
        ).model_dump(mode="json")


def test_context_provisions_before_codex_without_provider_token(monkeypatch, tmp_path):
    events = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASANA_TOKEN", "host-secret")
    monkeypatch.setenv("ACTIVE_WORK_ID", "stale")
    monkeypatch.setenv("REFERENCE_WORK_IDS", "stale")
    monkeypatch.setenv("SWITCHSTAND_PRIORITY_CLAIMS", "1")

    def fake_provision(repo, active, references, env):
        events.append(("provision", repo, active, references, dict(env)))
        return Authority(ACTIVE, (), (), UUID(int=2), 7)

    class Launcher:
        def run(self, **values):
            events.append(("managed", values))
            return {"state": "completed"}

    class Registry:
        def __init__(self, _root):
            pass

        def closure_gate(self):
            return True, ()

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
    monkeypatch.setattr(context, "ManagedParentLauncher", Launcher)
    monkeypatch.setattr(context, "PendingFailureRegistry", Registry)

    with pytest.raises(SystemExit) as stopped:
        context.run("1218242783900077", "repair the launcher")
    assert stopped.value.code == 0

    assert [event[0] for event in events] == ["preflight", "provision", "writer", "managed"]
    assert events[2][2] == ACTIVE
    provision_env = events[1][4]
    assert "ASANA_TOKEN" not in provision_env
    launch = events[3][1]
    assert launch["work_id"] == ACTIVE
    assert launch["grant_id"] == UUID(int=2)
    assert launch["grant_version"] == 7
    assert launch["writer"] == writer
    assert launch["codex_home"] == tmp_path / "codex"
    assert launch["assignment"] == "repair the launcher"
    assert launch["priority_claims"] is True


def test_context_work_id_launch_rejects_existing_legacy_gid_writer(monkeypatch, tmp_path):
    legacy = "1218242783900077"
    root = tmp_path / ".local/state/switchstand/writers"
    root.mkdir(parents=True, mode=0o700)
    (root / f"task-{legacy}").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(context, "prepared_check_environment", lambda *args: "uv")
    monkeypatch.setattr(context, "validate_control", lambda *args: tmp_path)
    monkeypatch.setattr(context, "_git", lambda *args, **kwargs: "head")
    monkeypatch.setattr(
        context, "provision", lambda *args: Authority(ACTIVE, (), (legacy,))
    )

    with pytest.raises(ValueError, match="legacy task writer exists"):
        context.run(str(ACTIVE), "assignment")


@pytest.mark.parametrize("assignment", ["inspect only", "Stop.\nDo not edit.\n`$HOME` 'quoted'"])
def test_parser_and_command_preserve_one_exact_initial_assignment(assignment):
    arguments = context.parser().parse_args(["--active", "123", "--", assignment])
    command = context.codex_command(
        Path("/control"), Path("/writer"), arguments.assignment[0], priority_claims=True,
    )
    assert command[-1].startswith(f"Exact launch assignment:\n{assignment}\n\n")
    assert command[-1].count(assignment) == 1
    assert "managed Worker context contract" in command[-1]
    assert "developer_instructions=" + json.dumps(MANAGED_DEVELOPER_INSTRUCTIONS) in command
    enabled = next(
        value for value in command
        if value.startswith("mcp_servers.switchstand.enabled_tools=")
    )
    assert "priority_claim_record" in enabled
    assert any("SWITCHSTAND_PRIORITY_CLAIMS" in value for value in command)
    default_enabled = next(
        value for value in context.codex_command(Path("/control"), Path("/writer"), assignment)
        if value.startswith("mcp_servers.switchstand.enabled_tools=")
    )
    assert "priority_claim_record" not in default_enabled
    assert not any("SWITCHSTAND_PRIORITY_CLAIMS" in value for value in
                   context.codex_command(Path("/control"), Path("/writer"), assignment))


@pytest.mark.parametrize(
    "arguments", [
        ["--active", "123"],
        ["--active", "123", "--", ""],
        ["--active", "123", "--", "one", "two"],
    ]
)
def test_parser_rejects_missing_or_multiple_initial_assignments(arguments):
    with pytest.raises(SystemExit):
        context.parser().parse_args(arguments)


def test_command_rejects_empty_initial_assignment():
    with pytest.raises(ValueError, match="must not be empty"):
        context.codex_command(Path("/control"), Path("/writer"), "")


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
        [launcher, "--active", "123", "--", "exact assignment"],
        env=os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}", "RESULT": str(result_file)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result_file.read_text().splitlines() == [
        "-m", "switchstand.context", "--active", "123", "--", "exact assignment"
    ]


def test_switchstand_isolated_dispatches_to_external_selector(tmp_path):
    scripts = tmp_path / "scripts"
    home = tmp_path / "home"
    selector = home / ".local" / "bin" / "switchstand-start"
    scripts.mkdir()
    selector.parent.mkdir(parents=True)
    launcher = scripts / "switchstand"
    launcher.write_bytes((Path(__file__).parents[1] / "scripts" / "switchstand").read_bytes())
    launcher.chmod(0o755)
    executable(
        selector,
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$RESULT\"\n",
    )
    result_file = tmp_path / "result"

    result = subprocess.run(
        [launcher, "--isolated", "--active", "123", "--commit", "a" * 40],
        env=os.environ | {"HOME": str(home), "RESULT": str(result_file)},
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
        assert arguments == [
            path, "--active", "1218483858041754", "--", "assignment",
        ]
        raise Reexec

    monkeypatch.setattr(context.os, "execv", execv)
    with pytest.raises(Reexec):
        context.run("1218483858041754", "assignment")


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


@pytest.mark.parametrize("external", [False, True])
def test_failed_private_writer_creation_leaves_canonical_path_retryable(monkeypatch, tmp_path, external):
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
    target = context.Target(control, git(control, "remote", "get-url", "origin"),
                            "marcogallotta/ai-tools", git(control, "rev-parse", "HEAD")) if external else None
    bind_identity = context._bind_git_identity

    def fail_identity(*args, **kwargs):
        raise RuntimeError("identity unavailable")

    monkeypatch.setattr(context, "_bind_git_identity", fail_identity)
    with pytest.raises(RuntimeError, match="identity unavailable"):
        context.create_writer(control, "1218438438638352", environment, target)

    root = context.durable_root(environment)
    assert sorted(path.name for path in root.iterdir()) == (
        ["task-1218438438638352.repository"] if external else [])
    monkeypatch.setattr(context, "_bind_git_identity", bind_identity)
    writer = context.create_writer(control, "1218438438638352", environment, target)
    assert writer == root / context._task_name("1218438438638352", target)
    assert context.validate_writer(control, writer, "1218438438638352", environment, target) == writer


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


def test_coordinator_hook_blocks_only_primary_git_mutations(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    git(primary, "init", "-b", "main")
    git(primary, "config", "user.name", "Test")
    git(primary, "config", "user.email", "test@example.invalid")
    (primary / "tracked.txt").write_text("base\n")
    git(primary, "add", "tracked.txt")
    git(primary, "commit", "-m", "base")
    writer = tmp_path / "writer"
    git(primary, "worktree", "add", "-b", "writer", str(writer))
    environment = dict(os.environ)

    for command in (
        "git status --short", "git config --get user.name", "git remote -v", "git notes",
        "git tag", "git tag --list", "git tag -l", "git fetch",
        "git branch -r --contains HEAD", "git branch -a --merged HEAD",
        f"git worktree add --detach {tmp_path / 'next'}",
        f"git -C {writer} reset --hard HEAD", "rm -rf build", "git push --force scratch",
    ):
        assert hook(primary, command, environment, coordinator_primary=primary) == {}

    for command in (
        "git add tracked.txt", "git reset --hard HEAD", "git clean -fd",
        "git branch topic", "git branch -r -d origin/topic", "git branch -r --del origin/topic",
        "git branch -a -M main",
        "git config user.name Changed", "git remote set-url origin nowhere",
        "git config --unset user.name", "git maintenance run", "git notes add -m note HEAD",
        "git tag release", "git tag -a release -m release",
    ):
        denied = hook(primary, command, environment, coordinator_primary=primary)
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "primary-checkout" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    foreign = tmp_path / "foreign"
    git(primary, "worktree", "add", "-b", "foreign", str(foreign))
    assert hook(writer, "git add tracked.txt", environment, coordinator_primary=primary, coordinator_writer=writer) == {}
    for repo, command in (
        (primary, f"env -C{foreign} git commit -m foreign"),
        (primary, f"env -vS'-C{foreign}' git commit -m foreign"),
        (primary, f"env -S '--chdir={foreign}' git commit -m foreign"),
    ):
        assert hook(repo, command, environment, coordinator_primary=primary,
                    coordinator_writer=writer) == {}
    patch = "*** Begin Patch\n*** Add File: probe\n+x\n*** End Patch"
    for tool, value in (("apply_patch", patch), ("Edit", str(foreign / "probe"))):
        assert hook(foreign, value, environment, tool=tool, coordinator_primary=primary,
                    coordinator_writer=writer) == {}
    legacy_store = tmp_path / ".local/state/switchstand/friction.md"
    current_store = tmp_path / ".local/state/switchstand/friction/friction.md"
    for root, store in ((primary, legacy_store), (writer, current_store)):
        (root / "friction.md").symlink_to(store)
        assert hook(root, "*** Update File: friction.md", environment | {"HOME": str(tmp_path)}, tool="apply_patch", coordinator_primary=primary, coordinator_writer=writer) == {}
        assert hook(root, str(root / "friction.md"), environment | {"HOME": str(tmp_path)}, tool="Edit", coordinator_primary=primary, coordinator_writer=writer) == {}
    friction_root = tmp_path / ".local/state/switchstand/friction"
    friction_root.mkdir(parents=True, exist_ok=True)
    for root in (primary, writer):
        category_root = root / "friction"
        if not category_root.exists():
            category_root.symlink_to(friction_root, target_is_directory=True)
        for name in ("capability.md", "process.md", "implementation.md"):
            (friction_root / name).touch(exist_ok=True)
            target = category_root / name
            assert hook(root, str(target), environment | {"HOME": str(tmp_path)},
                        tool="Edit", coordinator_primary=primary,
                        coordinator_writer=writer) == {}


def test_coordinator_hook_allows_only_friction_patch_in_primary(tmp_path):
    primary = tmp_path / "primary"
    writer = tmp_path / "writer"
    primary.mkdir()
    writer.mkdir()
    environment = dict(os.environ)

    friction = "*** Begin Patch\n*** Update File: friction.md\n@@\n-old\n+new\n*** End Patch"
    source = "*** Begin Patch\n*** Update File: src/main.py\n@@\n-old\n+new\n*** End Patch"
    outside = "*** Begin Patch\n*** Add File: created.txt\n+new\n*** End Patch"

    assert hook(primary, friction, environment, tool="apply_patch",
                coordinator_primary=primary) == {}
    denied = hook(primary, source, environment, tool="apply_patch",
                  coordinator_primary=primary)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert str(primary / "src/main.py") in denied["hookSpecificOutput"]["permissionDecisionReason"]
    (primary / "linked.py").symlink_to(writer / "target.py")
    linked = "*** Begin Patch\n*** Update File: linked.py\n@@\n-old\n+new\n*** End Patch"
    denied = hook(primary, linked, environment, tool="apply_patch",
                  coordinator_primary=primary)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert hook(writer, outside, environment, tool="apply_patch",
                coordinator_primary=primary) == {}


def test_coordinator_hook_denies_claude_edit_tools_in_primary_except_friction(tmp_path):
    primary = tmp_path / "primary"
    writer = tmp_path / "writer"
    primary.mkdir()
    writer.mkdir()
    environment = dict(os.environ)

    def edit(repo: Path, path: Path | str, tool: str) -> dict:
        result = subprocess.run(
            [str(Path(__file__).parents[1] / "scripts/codex-hook"),
             "--coordinator-primary", str(primary)],
            input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                              "tool_input": {"file_path": str(path)}, "cwd": str(repo)}),
            text=True, capture_output=True, check=True, env=environment,
        )
        return json.loads(result.stdout) if result.stdout else {}

    for tool in ("Write", "Edit", "MultiEdit"):
        denied = edit(primary, primary / "src/main.py", tool)
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "primary-checkout" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert edit(primary, primary / "friction.md", "Write") == {}
    assert edit(writer, writer / "src/main.py", "Write") == {}
    (primary / "src").mkdir()
    (tmp_path / "link").symlink_to(primary / "src", target_is_directory=True)
    denied = edit(writer, tmp_path / "link/x.py", "Write")
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert edit(primary, "src/rel.py", "Edit")["hookSpecificOutput"]["permissionDecision"] == "deny"
    notebook = subprocess.run(
        [str(Path(__file__).parents[1] / "scripts/codex-hook"),
         "--coordinator-primary", str(primary)],
        input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "NotebookEdit",
                          "tool_input": {"notebook_path": str(primary / "n.ipynb")},
                          "cwd": str(primary)}),
        text=True, capture_output=True, check=True, env=environment)
    assert "deny" in notebook.stdout
    broken = subprocess.run(
        [str(Path(__file__).parents[1] / "scripts/codex-hook"),
         "--coordinator-primary", str(primary)],
        input="not json", text=True, capture_output=True, check=True, env=environment)
    assert "guard failed closed" in broken.stdout


def test_hook_denies_edit_tools_writing_claude_auto_memory(tmp_path):
    home = tmp_path / "home"
    (tmp_path / "dotfiles/projects/p/memory").mkdir(parents=True)
    home.mkdir()
    (home / ".claude").symlink_to(tmp_path / "dotfiles", target_is_directory=True)
    environment = {**os.environ, "HOME": str(home)}

    def call(tool: str, path: Path) -> dict:
        field = "notebook_path" if tool == "NotebookEdit" else "file_path"
        result = subprocess.run(
            [str(Path(__file__).parents[1] / "scripts/codex-hook")],
            input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                              "tool_input": {field: str(path)}, "cwd": str(tmp_path)}),
            text=True, capture_output=True, check=True, env=environment)
        return json.loads(result.stdout) if result.stdout else {}

    memory = home / ".claude/projects/p/memory/MEMORY.md"
    for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        assert "memory-write" in call(tool, memory)["hookSpecificOutput"]["permissionDecisionReason"]
        assert call(tool, home / ".claude/projects/p/notes/memory/file.md") == {}
    assert call("Read", memory) == {}
    assert call("Write", home / ".claude/projects/p/other.json") == {}


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


@pytest.mark.parametrize("external", [False, True])
def test_managed_codex_home_has_only_control_hook_and_protected_auth(monkeypatch, tmp_path, external):
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

    target = context.Target(writer, "origin", "marcogallotta/ai-tools", "a" * 40) if external else None
    managed = context.managed_codex_home(control, writer, "1218438438638352",
                                         os.environ | {"HOME": str(tmp_path), "SWITCHSTAND_CHECK_UV": str(tmp_path / "pinned-uv")}, target)
    assert managed.name == context._task_name("1218438438638352", target)
    assert (managed / "repository").read_text().strip() == (target.repository if target else "switchstand")

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
    assert tomllib.loads(config)["developer_instructions"] == MANAGED_DEVELOPER_INSTRUCTIONS
    assert "[[hooks.SessionStart]]" not in config
    assert "coordinator-tracker-contract" not in config


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
        context.run("1218483858041754", "assignment")


def test_external_writer_uses_target_main_and_preserves_progress(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    git(target, "init", "-b", "main")
    git(target, "config", "user.name", "Test")
    git(target, "config", "user.email", "test@example.invalid")
    (target / "dish.txt").write_text("base")
    git(target, "add", ".")
    git(target, "commit", "-m", "base")
    base = git(target, "rev-parse", "HEAD")
    origin = "https://github.com/marcogallotta/ai-tools.git"
    git(target, "remote", "add", "origin", origin)
    (target / "dish.txt").write_text("dirty anchor")
    admitted = context.Target(target, origin, "marcogallotta/ai-tools", base)
    env = os.environ | {"HOME": str(tmp_path)}
    task = "123"
    writer = context.create_writer(target, task, env, admitted)
    assert writer.name == f"task-{task}-{admitted.fingerprint}"
    assert git(writer, "rev-parse", "HEAD") == base
    assert (writer / "dish.txt").read_text() == "base"
    assert git(writer, "remote", "get-url", "origin") == origin
    (target / "dish.txt").write_text("new main")
    git(target, "add", ".")
    git(target, "commit", "-m", "advance target")
    newer = context.Target(target, origin, admitted.repository, git(target, "rev-parse", "HEAD"))
    context.create_writer(target, task, env, newer)
    assert git(writer, "rev-parse", "HEAD") == newer.main
    (writer / "dish.txt").write_text("task progress")
    context.create_writer(target, task, env, admitted)
    assert (writer / "dish.txt").read_text() == "task progress"
    git(writer, "add", ".")
    git(writer, "commit", "-m", "task progress")
    checkpoint = git(writer, "rev-parse", "HEAD")
    context.create_writer(target, task, env, newer)
    assert git(writer, "rev-parse", "HEAD") == checkpoint
    with pytest.raises(ValueError, match="cross-target/mode"):
        context.create_writer(target, task, env)
    (writer / ".git/switchstand-repository").write_text("wrong/repo")
    with pytest.raises(ValueError, match="repository binding"):
        context.validate_writer(target, writer, task, env, newer)


@pytest.mark.parametrize("origin", ["/local/repo", "https://evil.test/marcogallotta/ai-tools.git",
    "https://github.com/other/ai-tools.git", "https://token@github.com/marcogallotta/ai-tools.git"])
def test_target_rejects_unsafe_origin_before_fetch(monkeypatch, tmp_path, origin):
    def read(repo, *args, env):
        if args == ("rev-parse", "--show-toplevel"):
            return str(tmp_path)
        assert args == ("remote", "get-url", "origin")
        return origin
    monkeypatch.setattr(context, "_git", read)
    with pytest.raises(ValueError):
        context.validate_target(tmp_path, "marcogallotta/ai-tools", {})


@pytest.mark.parametrize("target", [False, True])
def test_reexec_preserves_target(monkeypatch, tmp_path, target):
    monkeypatch.chdir(tmp_path)
    heads = iter(("a" * 40, "b" * 40))
    monkeypatch.setattr(context, "_git", lambda *a, **k: next(heads))
    monkeypatch.setattr(context, "validate_control", lambda root, env: root)
    def reexec(path, args):
        assert args == [path, "--active", "123", *(["--target-repo", str(tmp_path)]
                       if target else []), "--", "assignment"]
        raise RuntimeError("reexec")
    monkeypatch.setattr(context.os, "execv", reexec)
    with pytest.raises(RuntimeError, match="reexec"):
        context.run("123", "assignment", tmp_path if target else None)


def test_external_admission_failure_precedes_writer(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(context, "_git", lambda *a, **k: "a" * 40)
    monkeypatch.setattr(context, "validate_control", lambda root, env: root)
    monkeypatch.setattr(context, "prepared_check_environment", lambda *a: "/uv")
    def deny(*args):
        raise ValueError("current work repository admission failed")
    monkeypatch.setattr(context, "provision_target", deny)
    monkeypatch.setattr(context, "create_writer", lambda *a: pytest.fail("writer effect"))
    with pytest.raises(ValueError, match="admission"):
        context.run("123", "assignment", tmp_path)


def test_runtime_modes_cannot_share_task_state(tmp_path):
    root = context._private_directory(tmp_path / "runtime")
    target = context.Target(tmp_path, "origin", "marcogallotta/ai-tools", "a" * 40)
    context._task_mode(root, "123", target)
    context._task_mode(root, "123", target)
    with pytest.raises(ValueError, match="cross-target/mode"):
        context._task_mode(root, "123", None)
    (root / "task-456").mkdir()
    with pytest.raises(ValueError, match="cross-target/mode"):
        context._task_mode(root, "456", target)


def test_target_fetch_binds_remote_main_even_when_anchor_head_is_stale(monkeypatch, tmp_path):
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    git(anchor, "init", "-b", "main")
    git(anchor, "config", "user.name", "Test")
    git(anchor, "config", "user.email", "test@example.invalid")
    (anchor / "file").write_text("old")
    git(anchor, "add", ".")
    git(anchor, "commit", "-m", "old")
    old = git(anchor, "rev-parse", "HEAD")
    (anchor / "file").write_text("provider main")
    git(anchor, "commit", "-am", "new")
    accepted = git(anchor, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(anchor), str(remote))
    git(anchor, "reset", "--hard", old)
    (anchor / "file").write_text("dirty anchor")
    git(anchor, "remote", "add", "origin", "https://github.com/marcogallotta/ai-tools.git")
    real_git = context._git
    def local_transport(repo, *args, env):
        if args[:3] == ("fetch", "--no-tags", "origin"):
            args = (*args[:2], str(remote), *args[3:])
        return real_git(repo, *args, env=env)
    monkeypatch.setattr(context, "_git", local_transport)
    env = os.environ | {"HOME": str(tmp_path)}
    admitted = context.validate_target(anchor, "marcogallotta/ai-tools", env)
    assert admitted.main == accepted
    control = tmp_path / "control"
    control.mkdir()
    git(control, "init", "-b", "main")
    git(control, "config", "user.name", "Control")
    git(control, "config", "user.email", "control@example.invalid")
    git(control, "commit", "--allow-empty", "-m", "unrelated CONTROL")
    writer = context.create_writer(control, "123", env, admitted)
    assert git(writer, "config", "user.name") == "Control"
    assert git(writer, "rev-parse", "HEAD") == accepted
    assert (writer / "file").read_text() == "provider main"
    assert git(anchor, "rev-parse", "HEAD") == old
    assert (anchor / "file").read_text() == "dirty anchor"
