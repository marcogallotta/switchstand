import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from switchstand.launch import (
    PROFILE,
    clean_environment,
    linked_branch,
    parse_authority,
    provision,
    readback,
    validate_codex_args,
)

ACTIVE = UUID("00000000-0000-0000-0000-000000000001")
REFERENCE = UUID("00000000-0000-0000-0000-000000000002")


def test_clean_environment_removes_secret_and_stale_authority():
    source = {"PATH": "/bin", "ASANA_TOKEN": "secret", "ACTIVE_WORK_ID": "stale",
              "REFERENCE_WORK_IDS": "stale"}
    assert clean_environment(source) == {"PATH": "/bin"}


def test_linked_branch_requires_recorded_clean_green_head(monkeypatch, tmp_path):
    git_dir, common = tmp_path / "gitdir", tmp_path / "common"
    git_dir.mkdir()
    common.mkdir()
    head = "a" * 40
    (git_dir / "switchstand-green-sha").write_text(head)
    answers = iter((f"{git_dir}\n{common}\nowned\n{head}\n", ""))
    monkeypatch.setattr(subprocess, "run",
                        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0,
                                                                           stdout=next(answers)))
    assert linked_branch(tmp_path, {}) == "owned"
    answers = iter((f"{git_dir}\n{common}\nowned\n{head}\n", " M Dockerfile\n"))
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
    assert validate_codex_args(["--", "exec", "hello"]) == ["exec", "hello"]
    for arguments in (["--sandbox=danger-full-access"], ["-c", "sandbox_mode=read-only"]):
        with pytest.raises(ValueError):
            validate_codex_args(arguments)


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
