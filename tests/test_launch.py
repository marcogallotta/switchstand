import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from switchstand.launch import (
    PROFILE,
    clean_environment,
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
                              "sandbox": {"type": "workspaceWrite"},
                              "instructionSources": sources}},
    ]


@pytest.mark.parametrize("source, accepted", [("/global/AGENTS.md", True),
                                               ("/home/test/.claude/CLAUDE.md", False)])
def test_readback_allows_only_declared_instruction_sources(monkeypatch, source, accepted):
    monkeypatch.setattr(
        "switchstand.launch._rpc_messages",
        lambda repo, git_common_dir, env: readback_messages([source, "/repo/AGENTS.md"]),
    )
    if accepted:
        assert readback(Path("/repo"), Path("/repo/.git"), {}).profile == PROFILE
    else:
        with pytest.raises(RuntimeError, match="undeclared instruction"):
            readback(Path("/repo"), Path("/repo/.git"), {})
