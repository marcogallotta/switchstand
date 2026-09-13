import asyncio
import subprocess

import pytest

from switchstand import development
from switchstand.docker import DockerObject


def completed(args=(), stdout="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")


def tool(name):
    found = development.build_server()._tool_manager.get_tool(name)
    assert found is not None
    return found.fn


def owned(role: str, *, running=False, status="created", exit_code=0):
    return DockerObject(
        "container",
        f"switchstand-{role}-runid",
        "exact-id",
        "run-id",
        role,
        running,
        exit_code,
        status,
    )


def test_development_surface_is_closed():
    server = development.build_server()
    assert set(server._tool_manager._tools) == {
        "check",
        "quality",
        "commit_all_current_worktree",
        "run_status",
    }
    for item in server._tool_manager._tools.values():
        assert item.parameters.get("additionalProperties") is False


def test_unbound_development_surface_has_no_tools():
    assert not development.build_server(bound=False)._tool_manager._tools


async def test_focused_check_uses_exact_owned_container_and_bound(monkeypatch, tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_one.py").touch()
    monkeypatch.setattr(development, "_bound_repo", lambda: (tmp_path, "owned", "a" * 40))
    monkeypatch.setattr(development, "_manifest", lambda repo: "manifest")
    monkeypatch.setattr(development, "_run_owner", lambda repo, branch: "run-id")
    monkeypatch.setattr(
        development,
        "_git",
        lambda repo, *args: completed(stdout="a" * 40 + "\n"),
    )
    monkeypatch.setenv("SWITCHSTAND_QUALITY_IMAGE", "sha256:fixed")
    monkeypatch.setenv("SWITCHSTAND_QUALITY_NETWORK", "isolated")
    monkeypatch.setenv("SWITCHSTAND_MANIFEST_SHA256", "manifest")
    captured = {}

    async def fake_run(arguments, owner, role, timeout):
        captured.update(arguments=arguments, owner=owner, role=role, timeout=timeout)
        return completed(arguments)

    monkeypatch.setattr(development, "_run_owned_workload", fake_run)
    result = await tool("check")("a" * 40, ["tests/test_one.py"])
    command = captured["arguments"]
    assert result.status == "ok"
    assert captured["owner"] == "run-id" and captured["role"] == "check"
    assert captured["timeout"] == development.FOCUSED_SECONDS
    assert command[:3] == ["create", "--name", "switchstand-check-runid"]
    assert "sha256:fixed" in command and "isolated" in command
    assert "TEST_DATABASE_URL=" in " ".join(command)
    assert command[-1] == "tests/test_one.py"


async def test_focused_check_rejects_stale_dependency_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(development, "_bound_repo", lambda: (tmp_path, "owned", "a" * 40))
    monkeypatch.setattr(development, "_manifest", lambda repo: "changed")
    monkeypatch.setattr(
        development,
        "_git",
        lambda repo, *args: completed(stdout="a" * 40 + "\n"),
    )
    monkeypatch.setenv("SWITCHSTAND_MANIFEST_SHA256", "launched")
    result = await tool("check")("a" * 40, ["tests/test_one.py"])
    assert result.status == "stale"
    assert "relaunch required" in result.output


async def test_quality_uses_exact_owned_container_and_full_bound(monkeypatch, tmp_path):
    monkeypatch.setattr(development, "_bound_repo", lambda: (tmp_path, "owned", "a" * 40))
    monkeypatch.setattr(development, "_manifest", lambda repo: "manifest")
    monkeypatch.setattr(development, "_run_owner", lambda repo, branch: "run-id")
    monkeypatch.setattr(
        development,
        "_git",
        lambda repo, *args: completed(
            stdout=("a" * 40 + "\n") if "rev-parse" in args else ""
        ),
    )
    monkeypatch.setenv("SWITCHSTAND_MANIFEST_SHA256", "manifest")
    monkeypatch.setenv("SWITCHSTAND_QUALITY_IMAGE", "sha256:fixed")
    monkeypatch.setenv("SWITCHSTAND_QUALITY_NETWORK", "isolated")
    captured = {}

    async def fake_run(arguments, owner, role, timeout):
        captured.update(arguments=arguments, owner=owner, role=role, timeout=timeout)
        return completed(arguments)

    monkeypatch.setattr(development, "_run_owned_workload", fake_run)
    result = await tool("quality")("a" * 40)
    command = captured["arguments"]
    assert result.status == "ok"
    assert captured["role"] == "quality" and captured["timeout"] == development.QUALITY_SECONDS
    assert command[:3] == ["create", "--name", "switchstand-quality-runid"]
    assert "sha256:fixed" in command and f"{tmp_path}:/workspace:ro" in command
    assert "PYTHONPATH=/workspace/src" in " ".join(command)
    assert "compose.yaml" not in " ".join(command) and "Dockerfile" not in " ".join(command)


async def test_hung_workload_stops_cli_removes_exact_container_and_reraises(monkeypatch):
    class HangingProcess:
        def __init__(self):
            self.returncode = None
            self.stopped = False

        async def wait(self):
            if not self.stopped:
                await asyncio.Event().wait()
            self.returncode = -15
            return -15

        def terminate(self):
            self.stopped = True

        def kill(self):
            self.stopped = True

    process = HangingProcess()
    removed = []

    async def fake_create(args, owner, role):
        return owned(role)

    async def fake_subprocess(*args, **kwargs):
        return process

    monkeypatch.setattr(development, "_create_owned_container", fake_create)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(
        development,
        "remove_exact",
        lambda kind, object_id, owner, role, env: removed.append(
            (kind, object_id, owner, role)
        ),
    )
    with pytest.raises(TimeoutError):
        await development._run_owned_workload([], "run-id", "check", 0.001)
    assert removed == [("container", "exact-id", "run-id", "check")]


async def test_interrupted_workload_cancels_exact_daemon_container(monkeypatch):
    started = asyncio.Event()

    class HangingProcess:
        def __init__(self):
            self.returncode = None
            self.stopped = False

        async def wait(self):
            started.set()
            while not self.stopped:
                await asyncio.sleep(60)
            self.returncode = -15
            return -15

        def terminate(self):
            self.stopped = True

        def kill(self):
            self.stopped = True

    process = HangingProcess()
    removed = []

    async def fake_create(args, owner, role):
        return owned(role)

    async def fake_subprocess(*args, **kwargs):
        return process

    monkeypatch.setattr(development, "_create_owned_container", fake_create)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(
        development,
        "remove_exact",
        lambda kind, object_id, owner, role, env: removed.append(
            (kind, object_id, owner, role)
        ),
    )
    task = asyncio.create_task(development._run_owned_workload([], "run-id", "quality", 60))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert removed == [("container", "exact-id", "run-id", "quality")]


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (owned("check", running=True, status="running"), "without an exited daemon workload"),
        (owned("check", running=False, status="created"), "without an exited daemon workload"),
    ],
)
async def test_attach_end_without_exited_execution_removes_exact_container(
    monkeypatch, state, message
):
    class FinishedProcess:
        returncode = 0

        async def wait(self):
            return 0

    removed = []

    async def fake_create(args, owner, role):
        return owned(role)

    async def fake_subprocess(*args, **kwargs):
        return FinishedProcess()

    monkeypatch.setattr(development, "_create_owned_container", fake_create)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(development, "inspect_object", lambda kind, object_id, env: state)
    monkeypatch.setattr(
        development,
        "remove_exact",
        lambda kind, object_id, owner, role, env: removed.append(
            (kind, object_id, owner, role)
        ),
    )
    with pytest.raises(RuntimeError, match=message):
        await development._run_owned_workload([], "run-id", "check", 60)
    assert removed == [("container", "exact-id", "run-id", "check")]


async def test_success_uses_exact_daemon_exit_code_and_removes_bound_id(monkeypatch):
    class FinishedProcess:
        returncode = 99

        async def wait(self):
            return 99

    removed = []

    async def fake_create(args, owner, role):
        return owned(role)

    async def fake_subprocess(*args, **kwargs):
        return FinishedProcess()

    exited = owned("quality", running=False, status="exited", exit_code=7)
    monkeypatch.setattr(development, "_create_owned_container", fake_create)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(development, "inspect_object", lambda kind, object_id, env: exited)
    monkeypatch.setattr(
        development,
        "remove_exact",
        lambda kind, object_id, owner, role, env: removed.append(
            (kind, object_id, owner, role)
        ),
    )
    result = await development._run_owned_workload([], "run-id", "quality", 60)
    assert result.returncode == 7
    assert removed == [("container", "exact-id", "run-id", "quality")]


def test_development_environment_removes_credentials(monkeypatch):
    monkeypatch.setenv("ASANA_TOKEN", "secret")
    monkeypatch.setenv("GIT_CONFIG", "danger")
    monkeypatch.setenv("PATH", "/bin")
    clean = development._environment()
    assert clean["PATH"] == "/bin"
    assert "ASANA_TOKEN" not in clean and "GIT_CONFIG" not in clean


def test_credential_path_covers_environment_variants():
    assert all(
        development._credential_path(name)
        for name in (".env", ".env.local", "service.env.production")
    )
    assert not development._credential_path("environment.md")
    assert not development._credential_path("switchstand-config.example")


async def test_run_status_is_bound_to_owned_worktree(monkeypatch, tmp_path):
    monkeypatch.setattr(development, "_bound_repo", lambda: (tmp_path, "owned", "a" * 40))
    monkeypatch.setattr(
        development, "_git", lambda repo, *args: completed(stdout=str(tmp_path) + "\n")
    )
    captured = {}
    monkeypatch.setattr(
        development,
        "inspect_receipt",
        lambda path, repo, branch: captured.setdefault(
            "call", development.RunStatus(status="stopped")
        ),
    )
    result = await tool("run_status")()
    assert result.status == "stopped"
    assert captured["call"].status == "stopped"
