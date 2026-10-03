import os
import signal
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest
from chatgpt_fixture import service as chatgpt_service

from switchstand.chatgpt_mcp import build_ordinary_tools
from switchstand.codex_runtime import PROFILE
from switchstand.context import provision_target
from switchstand.development import DevelopmentBoundary
from switchstand.launch import (
    clean_environment,
    exact_revision_preflight,
    linked_branch,
    parse_authority,
    parser,
    prepare_managed_run,
    provision,
    run,
    supervise_codex,
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


def test_switchstand_tools_have_narrow_approval_free_policy():
    config = tomllib.loads((Path(__file__).parents[1] / ".codex/config.toml").read_text())
    assert config["approval_policy"] == "never"
    assert config["permissions"][PROFILE]["network"]["enabled"] is True
    servers = config["mcp_servers"]

    ordinary = {name for name, _ in build_ordinary_tools(chatgpt_service())}
    ordinary.add("outcome_state_update")
    switchstand = servers["switchstand"]
    assert switchstand["required"] is False
    assert switchstand["default_tools_approval_mode"] == "approve"
    assert "command" not in switchstand
    assert switchstand["url"] == "https://laptop.tail46f0b9.ts.net/switchstand/mcp"
    assert "oauth_resource" not in switchstand
    assert "auth" not in switchstand
    assert set(switchstand["enabled_tools"]) == ordinary
    assert "tools" not in switchstand
    assert servers["switchstand_oauth_proof"]["enabled"] is False
    certification = servers["switchstand_certification"]
    assert set(certification["enabled_tools"]) == ordinary

    managed = {
        "work_get", "work_history", "work_event", "work_append", "work_update", "message_pending",
        "message_receive", "message_recover", "message_result_send", "message_disposition",
    }
    switchstand_managed = servers["switchstand_managed"]
    assert switchstand_managed["required"] is False
    assert set(switchstand_managed["enabled_tools"]) == managed
    assert switchstand_managed["default_tools_approval_mode"] == "approve"
    assert "tools" not in switchstand_managed

    development = {
        "check",
        "commit_all_current_worktree",
        "diagnostic_full_suite",
        "run_status",
    }
    switchstand_development = servers["switchstand_development"]
    assert switchstand_development["required"] is False
    assert set(switchstand_development["enabled_tools"]) == development
    assert switchstand_development["default_tools_approval_mode"] == "approve"
    assert "tools" not in switchstand_development
    assert "SWITCHSTAND_RUN_ID" in switchstand_development["env_vars"]

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
    monkeypatch.setenv("SWITCHSTAND_RESOLVED_WORK_ID", str(ACTIVE))
    monkeypatch.setattr("switchstand.launch.linked_branch", lambda *_: "v2-task-foreign")
    monkeypatch.setattr(
        "switchstand.launch.readback",
        lambda *_: pytest.fail("readback must not precede exact task branch check"),
    )
    arguments = parser().parse_args(["--active", "9999999999999999", "--commit", "a" * 40])

    with pytest.raises(ValueError, match="does not match the exact active task"):
        run(arguments)


def test_parse_authority_requires_exact_complete_response():
    output = (f"build step=value\nACTIVE_WORK_ID={ACTIVE}\n"
              f"REFERENCE_WORK_IDS={REFERENCE}\nLEGACY_TASK_GIDS=123\n")
    parsed = parse_authority(output)
    assert parsed.active == ACTIVE
    assert parsed.legacy_task_gids == ("123",)
    with pytest.raises(ValueError):
        parse_authority(f"ACTIVE_WORK_ID={ACTIVE}\n")


@pytest.mark.parametrize("repository", [False, True])
def test_provision_passes_human_task_ids_and_surfaces_backup_receipt(
    monkeypatch, capsys, repository
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
            stdout=(f"ACTIVE_WORK_ID={ACTIVE}\nREFERENCE_WORK_IDS={REFERENCE}\n"
                    "LEGACY_TASK_GIDS=123\n"
                    + ("SWITCHSTAND_REPOSITORY=marcogallotta/ai-tools\n" if repository else "")),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    if repository:
        authority, admitted = provision_target(
            Path("/repo"), "123", ("456",), {"HOME": "/home/test"}
        )
        assert admitted == "marcogallotta/ai-tools"
    else:
        authority = provision(Path("/repo"), "123", ("456",), {"HOME": "/home/test"})
    assert authority.active == ACTIVE
    assert capsys.readouterr().out.endswith("backup /private/state.dump\n")
    state, _identity, upgrade, controller = captured
    assert state[0] == [
        "docker", "compose", "--project-directory", "/repo", "-f",
        "/repo/compose.state.yaml", "up", "-d", "--wait", "postgres",
    ]
    assert controller[0][controller[0].index("--active"):] == [
        "--active", "123", "--managed-agent",
        *(["--repository"] if repository else []), "--reference", "456",
    ]
    assert controller[0][2:6] == [
        "--project-directory", "/repo", "-f", "/repo/compose.yaml"
    ]
    assert upgrade[0] == [
        "/repo/scripts/switchstand-upgrade-state", "--target", "production"
    ]
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
            stdout=f"ACTIVE_WORK_ID={ACTIVE}\nREFERENCE_WORK_IDS=\nLEGACY_TASK_GIDS=123\n",
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


@pytest.mark.parametrize("provisioner", [provision, provision_target])
def test_provision_stops_on_state_upgrade_failure_with_exact_diagnostic(monkeypatch, provisioner):
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
        provisioner(Path("/repo"), "123", (), {"HOME": "/home/test"})

    assert not any("switchstand-provision" in command for command in calls)


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
        "switchstand.launch.reclaim_development",
        lambda *args, **kwargs: events.append("reclaimed"),
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
        control, candidate, "owned", "123", (), {}, tmp_path, ACTIVE
    )
    assert events == [
        "reserved",
        "reclaimed",
        "provisioned",
        "recorded",
        "development",
    ]
    assert result.authority is authority and result.development is development


def test_prepare_managed_run_rejects_resolved_work_id_mismatch(monkeypatch, tmp_path):
    events = []

    @contextmanager
    def reservation(*args):
        yield lambda active: events.append("recorded")

    monkeypatch.setattr("switchstand.launch.reserve_run", reservation)
    monkeypatch.setattr("switchstand.launch.reclaim_development", lambda *args: None)
    monkeypatch.setattr(
        "switchstand.launch.provision",
        lambda *args: type("Authority", (), {"active": ACTIVE})(),
    )
    monkeypatch.setattr(
        "switchstand.launch.prepare_development",
        lambda *args: events.append("development"),
    )

    with pytest.raises(ValueError, match="does not match"):
        prepare_managed_run(
            tmp_path, tmp_path, "owned", "legacy", (), {}, tmp_path,
            UUID("00000000-0000-0000-0000-000000000002"),
        )
    assert events == []


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
        lambda boundary, candidate, owner, env: events.append(
            (boundary.image, boundary.network, boundary.database, candidate, str(owner))
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
        lambda boundary, candidate, owner, env: events.append(
            (boundary.image, boundary.network, boundary.database, candidate, str(owner))
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


@pytest.mark.parametrize("provisioner", [provision, provision_target])
def test_provision_propagates_managed_provisioning_failure(monkeypatch, provisioner):
    def fake_run(command, **kwargs):
        if "switchstand-provision" in command:
            raise subprocess.CalledProcessError(17, command, stderr="admission denied")
        output = "a" * 40 + "\n/repo/.git\nHEAD\n" if command[0] == "git" else ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError) as failure:
        provisioner(Path("/repo"), "123", (), {})
    assert failure.value.returncode == 17
    assert failure.value.stderr == "admission denied"
