import asyncio
import subprocess
from ipaddress import ip_network
from pathlib import Path
from uuid import UUID

import pytest

from switchstand import development
from switchstand.docker import DockerObject

ACTIVE = UUID("00000000-0000-0000-0000-000000000001")


def completed(args=(), stdout="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")


def tool(name):
    found = development.build_server()._tool_manager.get_tool(name)
    assert found is not None
    return found.fn


def owned(role: str, *, running=False, status="created", exit_code=0):
    return DockerObject(
        "container",
        f"switchstand-{role}-run-id",
        "exact-id",
        "run-id",
        role,
        running,
        exit_code,
        status,
    )


def output_stream(value: bytes = b""):
    stream = asyncio.StreamReader()
    stream.feed_data(value)
    stream.feed_eof()
    return stream


def test_development_surface_is_closed():
    server = development.build_server()
    assert set(server._tool_manager._tools) == {
        "check",
        "diagnostic_full_suite",
        "commit_all_current_worktree",
        "run_status",
    }
    for item in server._tool_manager._tools.values():
        assert item.parameters.get("additionalProperties") is False


def test_unbound_development_surface_has_no_tools():
    assert not development.build_server(bound=False)._tool_manager._tools


def test_development_subnet_avoids_home_lan_space_and_is_identity_stable():
    repo = Path("/candidate")
    first = development.development_subnet(repo, UUID(int=1))
    second = development.development_subnet(repo, UUID(int=2))

    assert first == "10.248.248.0/24"
    assert second == "10.243.192.0/24"
    assert first == development.development_subnet(repo, UUID(int=1))
    assert first != second
    assert ip_network(first).subnet_of(ip_network("10.240.0.0/12"))
    assert ip_network(first).prefixlen == 24


def test_candidate_runner_context_excludes_hostile_build_and_migration_files(
    monkeypatch, tmp_path
):
    control, candidate = tmp_path / "control", tmp_path / "candidate"
    control.mkdir()
    candidate.mkdir()
    (control / "Dockerfile.candidate-runner").write_text("trusted\n")
    for name in ("pyproject.toml", "uv.lock", "README.md"):
        (candidate / name).write_text(f"candidate {name}\n")
    for name in ("Dockerfile", "compose.yaml", ".dockerignore", "alembic.ini"):
        (candidate / name).write_text("HOSTILE\n")
    (candidate / ".codex").mkdir()
    (candidate / ".codex" / "config.toml").write_text("HOSTILE\n")
    builds = []
    networks = []
    answers = iter(("sha256:image\n", "network-id\n", "database-id\n"))

    def docker(arguments, cwd, env, **kwargs):
        if arguments[0] == "build":
            builds.append((arguments, set(cwd.iterdir()), kwargs))
        elif arguments[:2] == ["network", "create"]:
            networks.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout=next(answers), stderr="")

    monkeypatch.setattr(development, "docker_run", docker)
    monkeypatch.setattr(development, "require_absent", lambda *args: None)
    monkeypatch.setattr(development, "require_owned", lambda *args: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )
    boundary = development.prepare_development(
        control, candidate, ACTIVE, {"PATH": "/bin"}
    )
    build, context_files, options = builds[0]
    assert build[0:3] == ["build", "--quiet", "--tag"]
    assert build[-3:] == ["-f", str(control / "Dockerfile.candidate-runner"), "."]
    assert "com.switchstand.run=" + str(ACTIVE) in build
    assert {path.name for path in context_files} == {"pyproject.toml", "uv.lock", "README.md"}
    assert options["timeout"] == 600
    assert len(networks) == 1
    network = networks[0]
    assert network[:-1] == [
        "network", "create", "--subnet",
        development.development_subnet(candidate, ACTIVE),
        "--label", f"com.switchstand.run={ACTIVE}",
        "--label", "com.switchstand.role=qualification",
    ]
    assert network[-1].startswith("switchstand-dev-")
    assert "--internal" not in network
    assert boundary == development.DevelopmentBoundary(
        "sha256:image", "network-id", "database-id", boundary.manifest
    )


def test_docker_failure_preserves_the_daemon_diagnostic(monkeypatch, tmp_path):
    monkeypatch.setattr(
        development,
        "docker_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 1, stdout="", stderr="could not find an available, non-overlapping IPv4 address pool"
        ),
    )
    with pytest.raises(RuntimeError, match="non-overlapping IPv4 address pool"):
        development.docker_run(
            ["network", "create", "--internal", "owned"], tmp_path, {}
        )


def test_cleanup_removes_only_the_exact_run_resources(monkeypatch, tmp_path):
    removed = []
    pruned = []
    monkeypatch.setattr(development, "inspect", lambda *args: None)
    monkeypatch.setattr(
        development,
        "remove_owned",
        lambda *args: removed.append(args),
    )
    monkeypatch.setattr(development, "prune_build_cache", lambda env: pruned.append(env))
    development.cleanup_development(
        development.DevelopmentBoundary(
            "image-id", "network-id", "database-id", "manifest"
        ),
        tmp_path,
        ACTIVE,
        {},
    )
    assert removed == [
        ("container", "database-id", str(ACTIVE), "database", {}),
        ("network", "network-id", str(ACTIVE), "qualification", {}),
        ("image", "image-id", str(ACTIVE), "runner", {}),
    ]
    assert pruned == [{}]


def test_cleanup_reconciles_named_resources_when_creation_lost_the_id(monkeypatch, tmp_path):
    removed = []
    monkeypatch.setattr(development, "prune_build_cache", lambda env: None)

    def inspect(kind, name, env):
        role = name.split("-")[1] if name.startswith("switchstand-focused-") else None
        if name.startswith("switchstand-quality-"):
            role = "quality"
        elif name.startswith("switchstand-test-"):
            role = "database"
        elif name.startswith("switchstand-dev-"):
            role = "qualification"
        elif name.startswith("switchstand-runner-"):
            role = "runner"
        assert role is not None
        return DockerObject(kind, name, f"{role}-id", str(ACTIVE), role)

    monkeypatch.setattr(development, "inspect", inspect)
    monkeypatch.setattr(
        development, "remove_owned", lambda *args: removed.append(args)
    )
    development.reclaim_development(tmp_path, ACTIVE, {})
    assert [(args[0], args[2], args[3]) for args in removed] == [
        ("container", str(ACTIVE), "database"),
        ("container", str(ACTIVE), "focused"),
        ("container", str(ACTIVE), "quality"),
        ("network", str(ACTIVE), "qualification"),
        ("image", str(ACTIVE), "runner"),
    ]


def test_cleanup_reports_build_cache_gc_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(development, "inspect", lambda *args: None)
    monkeypatch.setattr(
        development,
        "prune_build_cache",
        lambda env: (_ for _ in ()).throw(RuntimeError("GC unavailable")),
    )

    with pytest.raises(RuntimeError, match="development cleanup failed: GC unavailable"):
        development.reclaim_development(tmp_path, ACTIVE, {})


def test_build_cache_gc_has_a_bounded_runtime(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="exceeded its bounded runtime"):
        development.prune_build_cache({})


def test_development_setup_cleans_up_when_interrupted(monkeypatch, tmp_path):
    calls = 0
    cleaned = []

    def docker(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(args, 0, stdout="sha256:image\n", stderr="")

    monkeypatch.setattr(development, "docker_run", docker)
    monkeypatch.setattr(development, "require_absent", lambda *args: None)
    monkeypatch.setattr(development, "require_owned", lambda *args: None)
    monkeypatch.setattr(
        development,
        "_cleanup_development",
        lambda image, network, database, candidate, owner, env, **kwargs: cleaned.append(
            (image, network, database, candidate, owner)
        ),
    )
    for name in ("pyproject.toml", "uv.lock", "README.md"):
        (tmp_path / name).write_text(name)
    with pytest.raises(KeyboardInterrupt):
        development.prepare_development(tmp_path, tmp_path, ACTIVE, {})
    assert cleaned == [
        ("sha256:image", "sha256:image", None, tmp_path, str(ACTIVE))
    ]


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
    assert captured["owner"] == "run-id" and captured["role"] == "focused"
    assert captured["timeout"] == development.FOCUSED_SECONDS
    assert command[:3] == ["create", "--name", "switchstand-focused-run-id"]
    assert "sha256:fixed" in command and "isolated" in command
    assert "TEST_DATABASE_URL=" in " ".join(command)
    assert "com.switchstand.run=run-id" in command
    assert "com.switchstand.role=focused" in command
    assert all(
        value in command
        for value in (
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "512",
            "--memory",
            "2g",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec",
        )
    )
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


async def test_diagnostic_full_suite_is_labeled_and_uses_existing_bound_runner(
    monkeypatch, tmp_path
):
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
    result = await tool("diagnostic_full_suite")("a" * 40)
    command = captured["arguments"]
    assert result.status == "ok"
    assert result.output.startswith(development.DIAGNOSTIC_NOTICE)
    assert captured["role"] == "quality" and captured["timeout"] == development.QUALITY_SECONDS
    assert command[:3] == ["create", "--name", "switchstand-quality-run-id"]
    assert "sha256:fixed" in command and f"{tmp_path}:/workspace:ro" in command
    assert "PYTHONPATH=/workspace/src" in " ".join(command)
    assert "com.switchstand.run=run-id" in command
    assert "com.switchstand.role=quality" in command
    assert "compose.yaml" not in " ".join(command) and "Dockerfile" not in " ".join(command)


async def test_same_run_and_role_workloads_are_serialized(monkeypatch, tmp_path):
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
    monkeypatch.setenv("SWITCHSTAND_MANIFEST_SHA256", "manifest")
    monkeypatch.setenv("SWITCHSTAND_QUALITY_IMAGE", "sha256:fixed")
    monkeypatch.setenv("SWITCHSTAND_QUALITY_NETWORK", "isolated")
    second_ready = asyncio.Event()
    release_first = asyncio.Event()
    argument_calls = 0
    workload_calls = 0

    original_arguments = development._container_arguments

    def observed_arguments(repo, owner, role, paths):
        nonlocal argument_calls
        argument_calls += 1
        if argument_calls == 2:
            second_ready.set()
        return original_arguments(repo, owner, role, paths)

    async def fake_run(arguments, owner, role, timeout):
        nonlocal workload_calls
        workload_calls += 1
        if workload_calls == 1:
            await release_first.wait()
        return completed(arguments)

    monkeypatch.setattr(development, "_container_arguments", observed_arguments)
    monkeypatch.setattr(development, "_run_owned_workload", fake_run)
    server = development.build_server()
    check = server._tool_manager.get_tool("check")
    assert check is not None

    first = asyncio.create_task(check.fn("a" * 40, ["tests/test_one.py"]))
    second = asyncio.create_task(check.fn("a" * 40, ["tests/test_one.py"]))
    await second_ready.wait()
    assert workload_calls == 1

    release_first.set()
    results = await asyncio.gather(first, second)
    assert workload_calls == 2
    assert [result.status for result in results] == ["ok", "ok"]


async def test_cancel_during_post_create_inspection_removes_captured_id(monkeypatch):
    inspect_started = asyncio.Event()
    inspect_release = asyncio.Event()
    removed = []

    class CreatedProcess:
        returncode = 0

        async def communicate(self):
            return b"exact-id\n", b""

        def terminate(self):
            pass

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def fake_subprocess(*args, **kwargs):
        return CreatedProcess()

    async def fake_to_thread(function, *args):
        if function is development.require_absent:
            return None
        if function is development.inspect:
            inspect_started.set()
            await inspect_release.wait()
            return owned("focused")
        if function is development.remove_owned:
            removed.append((args[0], args[1], args[2], args[3]))
            return None
        return function(*args)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    task = asyncio.create_task(
        development._create_owned_container([], "run-id", "focused")
    )
    await inspect_started.wait()
    task.cancel()
    inspect_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert removed == [("container", "exact-id", "run-id", "focused")]


async def test_cancel_during_post_attach_readback_removes_captured_id(monkeypatch):
    inspect_started = asyncio.Event()
    inspect_release = asyncio.Event()
    removed = []

    class FinishedProcess:
        returncode = 0
        stdout = output_stream()

        async def wait(self):
            return 0

    async def fake_create(args, owner, role):
        return owned(role)

    async def fake_subprocess(*args, **kwargs):
        return FinishedProcess()

    async def fake_to_thread(function, *args):
        if function is development.inspect:
            inspect_started.set()
            await inspect_release.wait()
            return owned("quality", running=True, status="running")
        if function is development.remove_owned:
            removed.append((args[0], args[1], args[2], args[3]))
            return None
        return function(*args)

    monkeypatch.setattr(development, "_create_owned_container", fake_create)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    task = asyncio.create_task(
        development._run_owned_workload([], "run-id", "quality", 60)
    )
    await inspect_started.wait()
    task.cancel()
    inspect_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert removed == [("container", "exact-id", "run-id", "quality")]


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (owned("focused", running=True, status="running"), "without an exited daemon workload"),
        (owned("focused", running=False, status="created"), "without an exited daemon workload"),
    ],
)
async def test_attach_end_without_exited_execution_removes_exact_container(
    monkeypatch, state, message
):
    class FinishedProcess:
        returncode = 0
        stdout = output_stream()

        async def wait(self):
            return 0

    removed = []

    async def fake_create(args, owner, role):
        return owned(role)

    async def fake_subprocess(*args, **kwargs):
        return FinishedProcess()

    monkeypatch.setattr(development, "_create_owned_container", fake_create)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(development, "inspect", lambda kind, object_id, env: state)
    monkeypatch.setattr(
        development,
        "remove_owned",
        lambda kind, object_id, owner, role, env: removed.append(
            (kind, object_id, owner, role)
        ),
    )
    with pytest.raises(RuntimeError, match=message):
        await development._run_owned_workload([], "run-id", "focused", 60)
    assert removed == [("container", "exact-id", "run-id", "focused")]


async def test_output_overflow_stops_workload_and_removes_exact_container(
    monkeypatch,
):
    class NoisyProcess:
        def __init__(self):
            self.returncode = None
            self.stopped = False
            self.stdout = output_stream(b"x" * (development.WORKLOAD_OUTPUT_BYTES + 1))

        async def wait(self):
            while not self.stopped:
                await asyncio.sleep(60)
            self.returncode = -15
            return -15

        def terminate(self):
            self.stopped = True

        def kill(self):
            self.stopped = True

    process = NoisyProcess()
    removed = []

    async def fake_create(args, owner, role):
        return owned(role)

    async def fake_subprocess(*args, **kwargs):
        assert kwargs["stdout"] is asyncio.subprocess.PIPE
        assert kwargs["limit"] == development.WORKLOAD_OUTPUT_CHUNK_BYTES
        return process

    monkeypatch.setattr(development, "_create_owned_container", fake_create)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(
        development,
        "remove_owned",
        lambda kind, object_id, owner, role, env: removed.append(
            (kind, object_id, owner, role)
        ),
    )
    with pytest.raises(
        RuntimeError,
        match=(
            "output exceeded the 12000-byte hard limit; "
            "reduce test or tool output and rerun"
        ),
    ):
        await development._run_owned_workload([], "run-id", "quality", 60)
    assert process.stopped
    assert removed == [("container", "exact-id", "run-id", "quality")]


async def test_output_at_hard_limit_is_accepted():
    value = b"x" * development.WORKLOAD_OUTPUT_BYTES
    assert await development._read_bounded_output(output_stream(value)) == value.decode()


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
