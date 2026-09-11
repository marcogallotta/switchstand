import subprocess
import tomllib
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest

from switchstand.launch import (
    PROFILE,
    clean_environment,
    codex_command,
    linked_branch,
    parse_authority,
    prepare_managed_run,
    provision,
    readback,
    validate_codex_args,
)

ACTIVE = UUID("00000000-0000-0000-0000-000000000001")
REFERENCE = UUID("00000000-0000-0000-0000-000000000002")


def test_managed_tools_have_narrow_approval_free_policy():
    config = tomllib.loads((Path(__file__).parents[1] / ".codex/config.toml").read_text())
    servers = config["mcp_servers"]
    expected = {
        "switchstand": {"work_get", "work_update", "work_append"},
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
        assert names <= set(servers[server]["enabled_tools"])
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
    with pytest.raises(ValueError, match="clean green"):
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


def test_run_reservation_precedes_provision_and_development(monkeypatch, tmp_path):
    events = []

    @contextmanager
    def reservation(repo, branch, git_dir):
        events.append("reserved")

        def record(active_work_id):
            events.append("recorded")
            return object()

        yield record

    authority = type("Authority", (), {"active": ACTIVE})()
    development = object()
    monkeypatch.setattr("switchstand.launch.reserve_run", reservation)
    monkeypatch.setattr(
        "switchstand.launch.provision",
        lambda *args: events.append("provisioned") or authority,
    )
    monkeypatch.setattr(
        "switchstand.launch.prepare_development",
        lambda *args: events.append("development") or development,
    )
    result = prepare_managed_run(tmp_path, "owned", "123", (), {}, tmp_path)
    assert events == ["reserved", "provisioned", "development", "recorded"]
    assert result.authority is authority and result.development is development


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
