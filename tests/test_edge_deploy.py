import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

import switchstand.edge_deploy as subject

SHA = "a" * 40


def launcher(runtime: Path) -> str:
    return f'''#!/usr/bin/python
from pathlib import Path
RUNTIME = Path("{runtime}")
settings = {{"SWITCHSTAND_OUTCOME_STATE_ACTIONS": "1"}}
'''


def deployer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> subject.Deployer:
    monkeypatch.setattr(subject, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(subject, "LAUNCHER", tmp_path / "launcher")
    return subject.Deployer(tmp_path / "repo", SHA, Mock())


def test_candidate_preserves_exact_production_config() -> None:
    current = launcher(Path("/old/runtime"))
    _, rendered = subject.candidate_launcher(current, Path("/new/runtime"))
    assert '"SWITCHSTAND_OUTCOME_STATE_ACTIONS": "1"' in rendered
    assert rendered.replace("'/new/runtime'", '"/old/runtime"') == current


def test_authenticated_smoke_makes_one_logged_in_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token"
    token.write_text("secret")
    token.chmod(0o600)
    env = tmp_path / "edge.env"
    env.write_text(f"SWITCHSTAND_R0_TOKEN_FILE={token}\nSWITCHSTAND_R0_WORK_ID={'b' * 36}\n")
    env.chmod(0o600)
    client = Mock()
    client.call.return_value = {"status": "ok"}
    monkeypatch.setattr(subject, "ENV_FILE", env)
    monkeypatch.setattr(subject, "MCP", Mock(return_value=client))
    subject.authenticated_smoke()
    client.call.assert_called_once()
    assert client.call.call_args.args[1]["name"] == "work_get"


def test_prepare_reuses_matching_retained_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = deployer(tmp_path, monkeypatch)
    subject.LAUNCHER.write_text(launcher(tmp_path / "old"))
    target.runtime.mkdir(parents=True)
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def fake_git(path: Path, *arguments: str) -> subject.subprocess.CompletedProcess[str]:
        calls.append((path, arguments))
        output = SHA if arguments and arguments[0] == "rev-parse" else ""
        return subject.subprocess.CompletedProcess([], 0, output, "")

    monkeypatch.setattr(subject, "git", fake_git)
    monkeypatch.setattr(subject, "command", Mock(return_value=Mock(stdout="")))
    target.prepare()
    assert not any("worktree" in arguments for _, arguments in calls)


def test_restore_recreates_and_verifies_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = deployer(tmp_path, monkeypatch)
    target.database_backup = tmp_path / "backup.dump"
    target.database_backup.write_bytes(b"dump")
    metadata = {"db_migration_version": "0025", "data_fingerprints": {"WorkIds": "x"}}
    target.database_backup.with_suffix(".json").write_text(json.dumps(metadata))
    commands: list[list[str]] = []

    def fake_command(arguments: list[str], **_kwargs: object):
        commands.append(arguments)
        output = (
            json.dumps(metadata["data_fingerprints"]) if "fingerprints" in arguments else "0025"
        )
        return subject.subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(subject, "command", fake_command)
    monkeypatch.setattr(subject.subprocess, "run", Mock())
    target.restore_database()
    assert [command[3] for command in commands[:3]] == ["dropdb", "createdb", "psql"]
    assert commands[-1][1] == "fingerprints"


def test_rollback_uses_only_local_runtime_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = deployer(tmp_path, monkeypatch)
    target.attempt.mkdir(parents=True)
    target.launcher_before.write_text(launcher(tmp_path / "old"))
    target.cutover_started = True
    doctor = Mock(return_value=Mock(stdout=""))
    monkeypatch.setattr(target, "restore_database", Mock())
    service = Mock(side_effect=[Mock(stdout=value) for value in ("", "0", "", "42")])
    monkeypatch.setattr(target, "service", service)
    monkeypatch.setattr(subject, "git", Mock(return_value=Mock(stdout=SHA)))
    monkeypatch.setattr(subject, "command", doctor)
    monkeypatch.setattr(
        Path, "read_bytes", Mock(return_value=os.fsencode(subject.LAUNCHER) + b"\0")
    )
    monkeypatch.setattr(subject, "authenticated_smoke", Mock(side_effect=AssertionError))
    target.rollback()
    assert "--expected-sha" in doctor.call_args.args[0]
