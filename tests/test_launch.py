import os
import signal
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest

from switchstand.docker import DockerObject
from switchstand.launch import (
    PROFILE,
    DevelopmentBoundary,
    clean_environment,
    cleanup_development,
    codex_command,
    docker_run,
    exact_revision_preflight,
    filesystem_override,
    linked_branch,
    parse_authority,
    parser,
    prepare_development,
    prepare_managed_run,
    provision,
    readback,
    run,
    supervise_codex,
    validate_codex_args,
)

ACTIVE = UUID("00000000-0000-0000-0000-000000000001")
REFERENCE = UUID("00000000-0000-0000-0000-000000000002")


def test_exact_revision_preflight_rechecks_clean_current_main_and_provenance(
    monkeypatch, tmp_path
):
    candidate = tmp_path / "writer"
    control = tmp_path / "control"
    common = tmp_path / "control" / ".git"
    git_dir = common / "worktrees" / "writer"
    for path in (candidate, control, common, git_dir):
        path.mkdir(parents=True, exist_ok=True)
    requested = "a" * 40
    control_sha = "b" * 40
    commands = []
    state = {
        "observed": requested,
        "control_dirty": "",
        "candidate_dirty": "",
        "ancestor": True,
    }

    def fake_run(command, **kwargs):
        commands.append(command)
        cwd = kwargs.get("cwd")
        if command[1:3] == ["cat-file", "-t"]:
            return subprocess.CompletedProcess(command, 0, stdout="commit\n")
        if "--show-toplevel" in command:
            if cwd == control:
                return subprocess.CompletedProcess(
                    command, 0, stdout=f"{control}\n{common}\n{control_sha}\n"
                )
            return subprocess.CompletedProcess(
                command, 0,
                stdout=f"{candidate}\n{git_dir}\n{common}\n{state['observed']}\n",
            )
        if command[1:4] == ["worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"worktree {candidate}\n\n")
        if command[1:3] == ["rev-parse", "HEAD"] and cwd == candidate:
            return subprocess.CompletedProcess(
                command, 0, stdout=f"{state['observed']}\n"
            )
        if command[1:3] == ["status", "--porcelain"]:
            assert cwd in {candidate, control}
            dirty = state["candidate_dirty"] if cwd == candidate else state["control_dirty"]
            return subprocess.CompletedProcess(command, 0, stdout=dirty)
        if command[1:3] == ["merge-base", "--is-ancestor"]:
            assert command[-2:] == [control_sha, requested]
            return subprocess.CompletedProcess(command, 0 if state["ancestor"] else 1)
        raise AssertionError(command)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert exact_revision_preflight(
        control, candidate, requested, control_sha, str(common), {}
    ) == requested

    state["candidate_dirty"] = "?? untracked.py\n"
    with pytest.raises(ValueError, match="candidate must be clean"):
        exact_revision_preflight(control, candidate, requested, control_sha, str(common), {})
    assert exact_revision_preflight(
        control, candidate, requested, control_sha, str(common), {}, allow_dirty_task=True
    ) == requested
    state["observed"] = "c" * 40
    with pytest.raises(ValueError, match="exact requested revision"):
        exact_revision_preflight(
            control, candidate, requested, control_sha, str(common), {}, allow_dirty_task=True
        )
    state["candidate_dirty"] = ""
    with pytest.raises(ValueError, match="exact requested revision"):
        exact_revision_preflight(control, candidate, requested, control_sha, str(common), {})
    state["observed"] = requested
    state["ancestor"] = False
    with pytest.raises(ValueError, match="contain freshly fetched"):
        exact_revision_preflight(control, candidate, requested, control_sha, str(common), {})
    state["ancestor"] = True
    state["control_dirty"] = " M scripts/switchstand-start\n"
    with pytest.raises(ValueError, match="CONTROL checkout"):
        exact_revision_preflight(control, candidate, requested, control_sha, str(common), {})
    foreign_common = tmp_path / "foreign" / ".git"
    foreign_common.mkdir(parents=True)
    with pytest.raises(ValueError, match="provenance"):
        exact_revision_preflight(control, candidate, requested, control_sha, str(foreign_common), {})


@pytest.mark.parametrize(
    ("requested", "error"),
    [
        ("A" * 40, "exact lowercase"),
        ("a" * 39, "exact lowercase"),
    ],
)
def test_exact_revision_preflight_rejects_ambiguous_identity(tmp_path, requested, error):
    with pytest.raises(ValueError, match=error):
        exact_revision_preflight(
            tmp_path, tmp_path, requested, "b" * 40, str(tmp_path), {}
        )


def test_managed_tools_have_narrow_approval_free_policy():
    config = tomllib.loads((Path(__file__).parents[1] / ".codex/config.toml").read_text())
    assert config["permissions"][PROFILE]["network"]["enabled"] is True
    servers = config["mcp_servers"]
    expected = {
        "switchstand": {
            "work_get", "work_attachments", "source_task", "source_stories", "source_story",
            "work_history", "work_event", "work_append",
        },
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
    assert "SWITCHSTAND_RUN_ID" in servers["switchstand_development"]["env_vars"]


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
    with pytest.raises(ValueError, match="clean writer"):
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
        with pytest.raises(ValueError, match="clean writer"):
            linked_branch(tmp_path, {})


def test_dirty_task_branch_requires_exact_active_task_before_managed_effects(
    monkeypatch, tmp_path
):
    control, candidate = tmp_path / "control", tmp_path / "candidate"
    control.mkdir()
    candidate.mkdir()
    monkeypatch.chdir(control)
    monkeypatch.setenv("SWITCHSTAND_CONTROL_ROOT", str(control))
    monkeypatch.setenv("SWITCHSTAND_CANDIDATE_ROOT", str(candidate))
    monkeypatch.setattr("switchstand.launch.linked_branch", lambda *_: "v2-task-foreign")
    monkeypatch.setattr(
        "switchstand.launch.readback",
        lambda *_: pytest.fail("readback must not precede exact task branch check"),
    )
    arguments = parser().parse_args(["--active", "9999999999999999", "--commit", "a" * 40])

    with pytest.raises(ValueError, match="does not match the exact active task"):
        run(arguments)


def test_parse_authority_requires_exact_complete_response():
    output = f"build step=value\nACTIVE_WORK_ID={ACTIVE}\nREFERENCE_WORK_IDS={REFERENCE}\n"
    parsed = parse_authority(output)
    assert parsed.active == ACTIVE
    with pytest.raises(ValueError):
        parse_authority(f"ACTIVE_WORK_ID={ACTIVE}\n")


def test_provision_passes_human_task_ids_and_surfaces_backup_receipt(
    monkeypatch, capsys
):
    captured = []

    def fake_run(command, **kwargs):
        captured.append((command, kwargs))
        if "up" in command:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == "git":
            return subprocess.CompletedProcess(
                command, 0, stdout="a" * 40 + "\n/repo/.git\nHEAD\n", stderr=""
            )
        if command[0] == "/repo/scripts/switchstand-upgrade-state":
            return subprocess.CompletedProcess(
                command, 0,
                stdout=("state upgrade passed: 0004_required_result_persistence -> "
                        "0005_work_event_handles; preserved counts 1|2; "
                        "backup /private/state.dump\n"),
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"ACTIVE_WORK_ID={ACTIVE}\nREFERENCE_WORK_IDS={REFERENCE}\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    authority = provision(Path("/repo"), "123", ("456",), {"HOME": "/home/test"})
    assert authority.active == ACTIVE
    assert capsys.readouterr().out.endswith("backup /private/state.dump\n")
    state, _identity, upgrade, controller = captured
    assert state[0] == [
        "docker", "compose", "--project-directory", "/repo", "-f",
        "/repo/compose.state.yaml", "up", "-d", "--wait", "postgres",
    ]
    assert controller[0][-4:] == ["--active", "123", "--reference", "456"]
    assert controller[0][2:6] == [
        "--project-directory", "/repo", "-f", "/repo/compose.yaml"
    ]
    assert upgrade[0] == ["/repo/scripts/switchstand-upgrade-state"]
    assert upgrade[1]["env"] == {
        "HOME": "/home/test",
        "SWITCHSTAND_CONTROL_PATH": "/repo",
        "SWITCHSTAND_CONTROL_SHA": "a" * 40,
        "SWITCHSTAND_CONTROL_COMMON": "/repo/.git",
    }
    assert all(call[1]["cwd"] == Path("/repo") for call in captured)
    assert state[1]["env"] == controller[1]["env"] == {"HOME": "/home/test"}


def test_provision_does_not_pass_partial_selector_to_attached_control(monkeypatch):
    captured = []

    def fake_run(command, **kwargs):
        captured.append((command, kwargs))
        if "up" in command:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == "git":
            return subprocess.CompletedProcess(
                command, 0, stdout="a" * 40 + "\n/repo/.git\nmain\n", stderr=""
            )
        if command[0] == "/repo/scripts/switchstand-upgrade-state":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"ACTIVE_WORK_ID={ACTIVE}\nREFERENCE_WORK_IDS=\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    provision(
        Path("/repo"),
        "123",
        (),
        {"HOME": "/home/test", "SWITCHSTAND_CONTROL_SHA": "a" * 40},
    )

    upgrade = captured[2]
    assert upgrade[1]["env"] == {"HOME": "/home/test"}


def test_provision_stops_on_state_upgrade_failure_with_exact_diagnostic(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "up" in command:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == "git":
            return subprocess.CompletedProcess(
                command, 0, stdout="a" * 40 + "\n/repo/.git\nHEAD\n", stderr=""
            )
        return subprocess.CompletedProcess(
            command, 17, stdout="", stderr="unsupported shared schema: actual unexpected\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="unsupported shared schema: actual unexpected"):
        provision(Path("/repo"), "123", (), {"HOME": "/home/test"})

    assert not any("switchstand-provision" in command for command in calls)


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

    monkeypatch.setattr("switchstand.launch.docker_run", docker)
    monkeypatch.setattr("switchstand.launch.require_absent", lambda *args: None)
    monkeypatch.setattr("switchstand.launch.require_owned", lambda *args: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )
    boundary = prepare_development(control, candidate, ACTIVE, {"PATH": "/bin"})
    build, context_files, options = builds[0]
    assert build[0:3] == ["build", "--quiet", "--tag"]
    assert build[-3:] == ["-f", str(control / "Dockerfile.candidate-runner"), "."]
    assert "com.switchstand.run=" + str(ACTIVE) in build
    assert {path.name for path in context_files} == {"pyproject.toml", "uv.lock", "README.md"}
    assert options["timeout"] == 600
    assert len(networks) == 1
    network = networks[0]
    assert network[:-1] == [
        "network", "create",
        "--label", f"com.switchstand.run={ACTIVE}",
        "--label", "com.switchstand.role=qualification",
    ]
    assert network[-1].startswith("switchstand-dev-")
    assert "--internal" not in network
    assert boundary == DevelopmentBoundary(
        "sha256:image", "network-id", "database-id", boundary.manifest
    )


def test_validate_codex_args_blocks_boundary_overrides():
    assert validate_codex_args(["--", "do the work"]) == ["do the work"]
    for arguments in (["-sdanger-full-access"], ["-C/tmp"], ["-c", "sandbox_mode=read-only"]):
        with pytest.raises(ValueError):
            validate_codex_args(arguments)


def test_managed_codex_requires_both_mcp_servers():
    command = codex_command(Path("/control"), Path("/writer"), [])
    assert "mcp_servers.switchstand.required=true" in command
    assert "mcp_servers.switchstand_development.required=true" in command


def test_managed_codex_starts_work_without_a_manual_prompt():
    command = codex_command(Path("/control"), Path("/writer"), [])
    assert command[:-1] == [
        "codex", "-C", "/control", "--add-dir", "/writer", "-a", "never", "-c",
        f'default_permissions="{PROFILE}"',
        "-c", filesystem_override(Path("/control")),
        "-c", "mcp_servers.switchstand.required=true",
        "-c", "mcp_servers.switchstand_development.required=true",
    ]
    assert 'work_get(api_version="1")' in command[-1]
    assert "Active inbox" in command[-1]


@pytest.mark.parametrize("launch_request", ["", "inspect only", "Stop.\nDo not edit.\n`$HOME` 'quoted'"])
def test_managed_codex_preserves_launch_request_in_one_prompt(launch_request):
    default = codex_command(Path("/control"), Path("/writer"), [])
    command = codex_command(
        Path("/control"), Path("/writer"), validate_codex_args(["--", launch_request])
    )
    assert command[:-1] == default[:-1]
    prefix, supplied = command[-1].split("\n\nAdditional launch request:\n", 1)
    assert prefix == default[-1]
    assert supplied == launch_request


@pytest.mark.parametrize("arguments", [["--config=unsafe"], ["first", "second"]])
def test_managed_codex_command_rejects_extra_options_and_prompts(arguments):
    with pytest.raises(ValueError):
        codex_command(Path("/control"), Path("/writer"), arguments)


def test_run_reservation_precedes_provision_and_development(monkeypatch, tmp_path):
    events = []

    @contextmanager
    def reservation(repo, branch, git_dir, reclaim=None):
        events.append("reserved")
        assert reclaim is not None
        reclaim(type("Receipt", (), {"run_id": ACTIVE})())

        def record(active_work_id):
            events.append("recorded")
            return type("Receipt", (), {"run_id": ACTIVE})()

        yield record

    authority = type("Authority", (), {"active": ACTIVE})()
    development = object()
    monkeypatch.setattr("switchstand.launch.reserve_run", reservation)
    monkeypatch.setattr(
        "switchstand.launch.inspect_docker", lambda *args: None
    )
    monkeypatch.setattr(
        "switchstand.launch.provision",
        lambda *args: events.append("provisioned") or authority,
    )
    monkeypatch.setattr(
        "switchstand.launch.prepare_development",
        lambda *args: events.append("development") or development,
    )
    control = tmp_path / "control"
    candidate = tmp_path / "candidate"
    result = prepare_managed_run(
        control, candidate, "owned", "123", (), {}, tmp_path
    )
    assert events == [
        "reserved",
        "provisioned",
        "recorded",
        "development",
    ]
    assert result.authority is authority and result.development is development


def test_docker_failure_preserves_the_daemon_diagnostic(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "switchstand.launch.docker_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 1, stdout="", stderr="could not find an available, non-overlapping IPv4 address pool"
        )
    )
    with pytest.raises(RuntimeError, match="non-overlapping IPv4 address pool"):
        docker_run(["network", "create", "--internal", "owned"], tmp_path, {})


def test_cleanup_removes_only_the_exact_run_resources(monkeypatch, tmp_path):
    removed = []
    monkeypatch.setattr("switchstand.launch.inspect_docker", lambda *args: None)
    monkeypatch.setattr(
        "switchstand.launch.remove_owned",
        lambda *args: removed.append(args),
    )
    cleanup_development(
        "image-id", "network-id", "database-id", tmp_path, str(ACTIVE), {}
    )
    assert removed == [
        ("container", "database-id", str(ACTIVE), "database", {}),
        ("network", "network-id", str(ACTIVE), "qualification", {}),
        ("image", "image-id", str(ACTIVE), "runner", {}),
    ]


def test_cleanup_reconciles_named_resources_when_creation_lost_the_id(monkeypatch, tmp_path):
    removed = []

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

    monkeypatch.setattr("switchstand.launch.inspect_docker", inspect)
    monkeypatch.setattr(
        "switchstand.launch.remove_owned", lambda *args: removed.append(args)
    )
    cleanup_development(None, None, None, tmp_path, str(ACTIVE), {})
    assert [(args[0], args[2], args[3]) for args in removed] == [
        ("container", str(ACTIVE), "database"),
        ("container", str(ACTIVE), "focused"),
        ("container", str(ACTIVE), "quality"),
        ("network", str(ACTIVE), "qualification"),
        ("image", str(ACTIVE), "runner"),
    ]


def test_development_setup_cleans_up_when_interrupted(monkeypatch, tmp_path):
    calls = 0
    cleaned = []

    def docker(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(args, 0, stdout="sha256:image\n", stderr="")

    monkeypatch.setattr("switchstand.launch.docker_run", docker)
    monkeypatch.setattr("switchstand.launch.require_absent", lambda *args: None)
    monkeypatch.setattr("switchstand.launch.require_owned", lambda *args: None)
    monkeypatch.setattr(
        "switchstand.launch.cleanup_development",
        lambda image, network, database, candidate, owner, env, **kwargs: cleaned.append(
            (image, network, database, candidate, owner)
        ),
    )
    for name in ("pyproject.toml", "uv.lock", "README.md"):
        (tmp_path / name).write_text(name)
    with pytest.raises(KeyboardInterrupt):
        prepare_development(tmp_path, tmp_path, ACTIVE, {})
    assert cleaned == [
        ("sha256:image", "sha256:image", None, tmp_path, str(ACTIVE))
    ]


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
        lambda image, network, database, candidate, owner, env: events.append(
            (image, network, database, candidate, owner)
        ),
    )
    development = DevelopmentBoundary("image", "network", "database", "manifest")
    assert supervise_codex(
        ["codex"], {"SWITCHSTAND_WORKTREE": "/writer"}, development, ACTIVE
    ) == 128 + signal.SIGTERM
    assert launch["start_new_session"] is True
    assert set(handlers) >= {
        signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM,
        signal.SIGTSTP, signal.SIGCONT, signal.SIGWINCH,
    }
    assert events == [
        (123, signal.SIGTERM),
        "waited",
        ("image", "network", "database", Path("/writer"), str(ACTIVE)),
    ]


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
        lambda image, network, database, candidate, owner, env: events.append(
            (image, network, database, candidate, owner)
        ),
    )
    development = DevelopmentBoundary("image", "network", "database", "manifest")
    assert supervise_codex(
        ["codex"], {"SWITCHSTAND_WORKTREE": "/writer"}, development, ACTIVE
    ) == 128 + signal.SIGQUIT
    assert events == [
        (123, signal.SIGQUIT),
        (123, signal.SIGKILL),
        ("image", "network", "database", Path("/writer"), str(ACTIVE)),
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
    assert supervise_codex(
        [sys.executable, "-c", code],
        dict(os.environ) | {"SWITCHSTAND_WORKTREE": str(tmp_path)},
        development,
        ACTIVE,
    ) == 0
    blocked = {int(item) for item in output.read_text().split(",") if item}
    assert not blocked.intersection({signal.SIGINT, signal.SIGQUIT, signal.SIGTERM})


def readback_messages(sources):
    return [
        {"id": 2, "result": {"data": [{"id": PROFILE, "allowed": True}]}},
        {"id": 3, "result": {"activePermissionProfile": {"id": PROFILE},
                              "sandbox": {"type": "workspaceWrite", "networkAccess": True,
                                          "writableRoots": ["/writer"]},
                              "runtimeWorkspaceRoots": ["/repo", "/writer"],
                              "approvalPolicy": "never",
                              "instructionSources": sources}},
    ]


def test_readback_accepts_profile_when_codex_omits_allowed(monkeypatch):
    messages = readback_messages(
        [str(Path.home() / ".codex/AGENTS.md"), "/repo/AGENTS.md"]
    )
    del messages[0]["result"]["data"][0]["allowed"]
    monkeypatch.setattr(
        "switchstand.launch._rpc_messages",
        lambda control, candidate, env: messages,
    )
    assert readback(Path("/repo"), Path("/writer"), {}).profile == PROFILE


def test_readback_rejects_explicitly_disallowed_profile(monkeypatch):
    messages = readback_messages(
        [str(Path.home() / ".codex/AGENTS.md"), "/repo/AGENTS.md"]
    )
    messages[0]["result"]["data"][0]["allowed"] = False
    monkeypatch.setattr(
        "switchstand.launch._rpc_messages",
        lambda control, candidate, env: messages,
    )
    with pytest.raises(RuntimeError, match="permission profile.*not available"):
        readback(Path("/repo"), Path("/writer"), {})


@pytest.mark.parametrize("source, accepted", [(str(Path.home() / ".codex/AGENTS.md"), True),
                                               ("/home/test/.claude/CLAUDE.md", False)])
def test_readback_allows_only_declared_instruction_sources(monkeypatch, source, accepted):
    monkeypatch.setattr(
        "switchstand.launch._rpc_messages",
        lambda control, candidate, env: readback_messages([source, "/repo/AGENTS.md"]),
    )
    if accepted:
        assert readback(Path("/repo"), Path("/writer"), {}).profile == PROFILE
    else:
        with pytest.raises(RuntimeError, match="undeclared instruction"):
            readback(Path("/repo"), Path("/writer"), {})


def test_readback_rejects_control_or_other_writable_roots(monkeypatch):
    messages = readback_messages([str(Path.home() / ".codex/AGENTS.md"), "/repo/AGENTS.md"])
    messages[1]["result"]["sandbox"]["writableRoots"] = ["/repo", "/writer"]
    monkeypatch.setattr(
        "switchstand.launch._rpc_messages",
        lambda control, candidate, env: messages,
    )
    with pytest.raises(RuntimeError, match="unexpected writable roots"):
        readback(Path("/repo"), Path("/writer"), {})


def test_readback_rejects_disabled_network(monkeypatch):
    messages = readback_messages([str(Path.home() / ".codex/AGENTS.md"), "/repo/AGENTS.md"])
    messages[1]["result"]["sandbox"]["networkAccess"] = False
    monkeypatch.setattr(
        "switchstand.launch._rpc_messages",
        lambda control, candidate, env: messages,
    )
    with pytest.raises(RuntimeError, match="unexpected sandbox"):
        readback(Path("/repo"), Path("/writer"), {})
