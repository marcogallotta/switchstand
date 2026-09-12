import os
import signal
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest

from switchstand.launch import (
    PROFILE,
    DevelopmentBoundary,
    clean_environment,
    cleanup_development,
    codex_command,
    development_names,
    docker_run,
    linked_branch,
    parse_authority,
    prepare_development,
    prepare_managed_run,
    provision,
    readback,
    supervise_codex,
    validate_codex_args,
)

ACTIVE = UUID("00000000-0000-0000-0000-000000000001")
REFERENCE = UUID("00000000-0000-0000-0000-000000000002")


def test_managed_tools_have_narrow_approval_free_policy():
    config = tomllib.loads((Path(__file__).parents[1] / ".codex/config.toml").read_text())
    servers = config["mcp_servers"]
    expected = {
        "switchstand": {"work_get", "source_task", "source_stories", "source_story", "work_append"},
        "switchstand_development": {
            "check",
            "commit_all_current_worktree",
            "quality",
            "run_status",
        },
    }
    for server, names in expected.items():
        assert servers[server]["required"] is False
        tools = servers[server]["tools"]
        assert set(tools) == names
        assert names == set(servers[server]["enabled_tools"])
        assert all(tool["approval_mode"] == "approve" for tool in tools.values())


def test_clean_environment_removes_secret_and_stale_authority():
    source = {"PATH": "/bin", "DOCKER_HOST": "remote", "ASANA_TOKEN": "secret", "ACTIVE_WORK_ID": "stale",
              "REFERENCE_WORK_IDS": "stale", "SWITCHSTAND_MANAGED": "1"}
    assert clean_environment(source) == {"PATH": "/bin"}


def test_linked_branch_requires_recorded_clean_green_head(monkeypatch, tmp_path):
    git_dir, common = tmp_path / "gitdir", tmp_path / "common"
    git_dir.mkdir()
    common.mkdir()
    head = "a" * 40
    (git_dir / "switchstand-green-sha").write_text(head)
    answers = iter((f"{git_dir}\n{common}\n{head}\nowned\n", ""))
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=next(answers))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert linked_branch(tmp_path, {}) == "owned"
    assert commands[0][-3:] == ["HEAD", "--abbrev-ref", "HEAD"]
    answers = iter((f"{git_dir}\n{common}\n{head}\nowned\n", " M Dockerfile\n"))
    with pytest.raises(ValueError, match="clean task"):
        linked_branch(tmp_path, {})


@pytest.mark.parametrize("ancestor", [True, False])
def test_linked_branch_checks_clean_commits_after_green_head(
    monkeypatch, tmp_path, ancestor
):
    git_dir, common = tmp_path / "gitdir", tmp_path / "common"
    git_dir.mkdir()
    common.mkdir()
    green, head = "a" * 40, "b" * 40
    (git_dir / "switchstand-green-sha").write_text(green)

    def fake_run(command, **kwargs):
        if "--is-ancestor" in command:
            assert command[-2:] == [green, head]
            return subprocess.CompletedProcess(command, 0 if ancestor else 1)
        if "--porcelain" in command:
            return subprocess.CompletedProcess(command, 0, stdout="")
        return subprocess.CompletedProcess(
            command, 0, stdout=f"{git_dir}\n{common}\n{head}\nowned\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    if ancestor:
        assert linked_branch(tmp_path, {}) == "owned"
    else:
        with pytest.raises(ValueError, match="clean task"):
            linked_branch(tmp_path, {})


def test_parse_authority_requires_exact_complete_response():
    output = f"build step=value\nACTIVE_WORK_ID={ACTIVE}\nREFERENCE_WORK_IDS={REFERENCE}\n"
    parsed = parse_authority(output)
    assert parsed.active == ACTIVE
    with pytest.raises(ValueError):
        parse_authority(f"ACTIVE_WORK_ID={ACTIVE}\n")


def test_provision_passes_human_task_ids_without_provider_credentials(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"ACTIVE_WORK_ID={ACTIVE}\nREFERENCE_WORK_IDS={REFERENCE}\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    authority = provision(Path("/repo"), "123", ("456",), {"HOME": "/home/test"})
    assert authority.active == ACTIVE
    assert captured["command"][-4:] == ["--active", "123", "--reference", "456"]
    assert captured["kwargs"]["env"] == {"HOME": "/home/test"}


def test_validate_codex_args_blocks_boundary_overrides():
    assert validate_codex_args(["--", "do the work"]) == ["do the work"]
    for arguments in (["-sdanger-full-access"], ["-C/tmp"], ["-c", "sandbox_mode=read-only"]):
        with pytest.raises(ValueError):
            validate_codex_args(arguments)


def test_managed_codex_requires_both_mcp_servers():
    command = codex_command(Path("/writer"), [])
    assert "mcp_servers.switchstand.required=true" in command
    assert "mcp_servers.switchstand_development.required=true" in command


def test_managed_codex_starts_work_without_a_manual_prompt():
    command = codex_command(Path("/writer"), [])
    assert command[:-1] == [
        "codex", "-C", "/writer", "-a", "never", "-c",
        f'default_permissions="{PROFILE}"',
        "-c", "mcp_servers.switchstand.required=true",
        "-c", "mcp_servers.switchstand_development.required=true",
    ]
    assert 'work_get(api_version="1")' in command[-1]
    assert "Active inbox" in command[-1]


@pytest.mark.parametrize("request", ["", "inspect only", "Stop.\nDo not edit.\n`$HOME` 'quoted'"])
def test_managed_codex_preserves_launch_request_in_one_prompt(request):
    default = codex_command(Path("/writer"), [])
    command = codex_command(Path("/writer"), validate_codex_args(["--", request]))
    assert command[:-1] == default[:-1]
    prefix, supplied = command[-1].split("\n\nAdditional launch request:\n", 1)
    assert prefix == default[-1]
    assert supplied == request


@pytest.mark.parametrize("arguments", [["--config=unsafe"], ["first", "second"]])
def test_managed_codex_command_rejects_extra_options_and_prompts(arguments):
    with pytest.raises(ValueError):
        codex_command(Path("/writer"), arguments)


def test_run_reservation_precedes_provision_and_development(monkeypatch, tmp_path):
    events = []

    @contextmanager
    def reservation(repo, branch, git_dir, reclaim=None):
        events.append("reserved")
        assert reclaim is not None
        reclaim(type("Receipt", (), {"pid": 123})())

        def record(active_work_id):
            events.append("recorded")
            return object()

        yield record

    authority = type("Authority", (), {"active": ACTIVE})()
    development = object()
    monkeypatch.setattr("switchstand.launch.reserve_run", reservation)
    monkeypatch.setattr(
        "switchstand.launch.cleanup_development",
        lambda network, database, env: events.append((network, database)),
    )
    monkeypatch.setattr(
        "switchstand.launch.provision",
        lambda *args: events.append("provisioned") or authority,
    )
    monkeypatch.setattr(
        "switchstand.launch.prepare_development",
        lambda *args: events.append("development") or development,
    )
    result = prepare_managed_run(tmp_path, "owned", "123", (), {}, tmp_path)
    assert events == [
        "reserved",
        development_names(tmp_path, 123),
        "provisioned",
        "recorded",
        "development",
    ]
    assert result.authority is authority and result.development is development


def test_docker_failure_preserves_the_daemon_diagnostic(monkeypatch, tmp_path):
    def failed(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command, stderr="could not find an available, non-overlapping IPv4 address pool"
        )

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(RuntimeError, match="non-overlapping IPv4 address pool"):
        docker_run(["network", "create", "--internal", "owned"], tmp_path, {})


def test_cleanup_removes_only_the_exact_run_resources(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cleanup_development("switchstand-dev-owned", "switchstand-test-owned", {})
    assert commands == [
        ["docker", "rm", "-f", "switchstand-test-owned"],
        ["docker", "network", "rm", "switchstand-dev-owned"],
    ]


def test_development_setup_cleans_up_when_interrupted(monkeypatch, tmp_path):
    calls = 0
    cleaned = []

    def docker(*args):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(args, 0, stdout="sha256:image\n", stderr="")

    monkeypatch.setattr("switchstand.launch.docker_run", docker)
    monkeypatch.setattr(
        "switchstand.launch.cleanup_development",
        lambda network, database, env, **kwargs: cleaned.append((network, database)),
    )
    with pytest.raises(KeyboardInterrupt):
        prepare_development(tmp_path, {})
    assert cleaned == [development_names(tmp_path, os.getpid())]


def test_supervisor_forwards_termination_and_always_cleans_up(monkeypatch):
    events = []
    handlers = {}
    launch = {}

    class Process:
        pid = 123

        def wait(self, timeout=None):
            events.append("waited")
            return -signal.SIGTERM

    def popen(*args, **kwargs):
        assert signal.SIGTERM in handlers
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        launch.update(kwargs)
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler))
    monkeypatch.setattr(os, "killpg", lambda pid, sig: events.append((pid, sig)))
    monkeypatch.setattr(
        "switchstand.launch.cleanup_development",
        lambda network, database, env: events.append((network, database)),
    )
    development = DevelopmentBoundary("image", "network", "database", "manifest")
    assert supervise_codex(["codex"], {}, development) == 128 + signal.SIGTERM
    assert launch["start_new_session"] is True
    assert set(handlers) >= {
        signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM,
        signal.SIGTSTP, signal.SIGCONT, signal.SIGWINCH,
    }
    assert events == [(123, signal.SIGTERM), "waited", ("network", "database")]


def test_supervisor_kills_unresponsive_child_before_cleanup(monkeypatch):
    events = []
    handlers = {}
    ticks = iter((10.0, 12.0))

    class Process:
        pid = 123
        started = False

        def wait(self, timeout=None):
            if not self.started:
                self.started = True
                handlers[signal.SIGQUIT](signal.SIGQUIT, None)
            if (123, signal.SIGKILL) not in events:
                raise subprocess.TimeoutExpired("codex", timeout)
            return -signal.SIGKILL

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler))
    monkeypatch.setattr("switchstand.launch.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr(os, "killpg", lambda pid, sig: events.append((pid, sig)))
    monkeypatch.setattr(
        "switchstand.launch.cleanup_development",
        lambda network, database, env: events.append((network, database)),
    )
    development = DevelopmentBoundary("image", "network", "database", "manifest")
    assert supervise_codex(["codex"], {}, development) == 128 + signal.SIGQUIT
    assert events == [
        (123, signal.SIGQUIT),
        (123, signal.SIGKILL),
        ("network", "database"),
    ]


def test_supervised_child_inherits_unblocked_forwarded_signals(monkeypatch, tmp_path):
    output = tmp_path / "blocked-signals"
    code = (
        "import signal; from pathlib import Path; "
        f"Path({str(output)!r}).write_text(','.join(map(str, "
        "sorted(signal.pthread_sigmask(signal.SIG_BLOCK, set())))))"
    )
    monkeypatch.setattr("switchstand.launch.cleanup_development", lambda *args: None)
    development = DevelopmentBoundary("image", "network", "database", "manifest")
    assert supervise_codex([sys.executable, "-c", code], dict(os.environ), development) == 0
    blocked = {int(item) for item in output.read_text().split(",") if item}
    assert not blocked.intersection({signal.SIGINT, signal.SIGQUIT, signal.SIGTERM})


def readback_messages(sources):
    return [
        {"id": 2, "result": {"data": [{"id": PROFILE, "allowed": True}]}},
        {"id": 3, "result": {"activePermissionProfile": {"id": PROFILE},
                              "sandbox": {"type": "workspaceWrite", "networkAccess": False},
                              "approvalPolicy": "never",
                              "instructionSources": sources}},
    ]


@pytest.mark.parametrize("source, accepted", [(str(Path.home() / ".codex/AGENTS.md"), True),
                                               ("/home/test/.claude/CLAUDE.md", False)])
def test_readback_allows_only_declared_instruction_sources(monkeypatch, source, accepted):
    monkeypatch.setattr(
        "switchstand.launch._rpc_messages",
        lambda repo, env: readback_messages([source, "/repo/AGENTS.md"]),
    )
    if accepted:
        assert readback(Path("/repo"), {}).profile == PROFILE
    else:
        with pytest.raises(RuntimeError, match="undeclared instruction"):
            readback(Path("/repo"), {})
