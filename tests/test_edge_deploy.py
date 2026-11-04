import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

import switchstand.edge_deploy as subject

SHA = "a" * 40


def launcher(runtime: Path) -> str:
    return f'''#!/usr/bin/python
import os
from pathlib import Path
RUNTIME = Path("{runtime}")
settings = {{"SWITCHSTAND_OUTCOME_STATE_ACTIONS": "1"}}

def run():
    oauth = {{}}
    os.environ.update(oauth)
'''


def deployer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> subject.Deployer:
    monkeypatch.setattr(subject, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(subject, "LAUNCHER", tmp_path / "launcher")
    return subject.Deployer(tmp_path / "repo", SHA, Mock())


def test_candidate_preserves_exact_production_config() -> None:
    current = launcher(Path("/old/runtime"))
    _, rendered = subject.candidate_launcher(current, Path("/new/runtime"), SHA)
    assert '"SWITCHSTAND_OUTCOME_STATE_ACTIONS": "1"' in rendered
    assert "SWITCHSTAND_PRODUCT_CURRENTNESS_RUNTIME_SHA" in rendered
    assert f'"SWITCHSTAND_PRODUCT_CURRENTNESS_RUNTIME_SHA": "{SHA}"' in rendered
    assert f'"SWITCHSTAND_PRODUCT_CURRENTNESS_SELECTED_RUNTIME_SHA": "{SHA}"' in rendered
    assert '"SWITCHSTAND_PRODUCT_CURRENTNESS_RUN_ID": "edge-deploy-aaaaaaaaaaaa"' in rendered


def test_candidate_replaces_prior_deployed_currentness_identity() -> None:
    current = launcher(Path("/old/runtime"))
    _, first = subject.candidate_launcher(current, Path("/first/runtime"), "b" * 40)
    _, second = subject.candidate_launcher(first, Path("/second/runtime"), SHA)
    assert second.count(subject.CURRENTNESS_START) == 1
    assert second.count(subject.CURRENTNESS_END) == 1
    assert "b" * 40 not in second
    assert f'"SWITCHSTAND_PRODUCT_CURRENTNESS_RUNTIME_SHA": "{SHA}"' in second


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
    client.client = Mock()
    client.call.return_value = {"status": "ok"}
    monkeypatch.setattr(subject, "ENV_FILE", env)
    monkeypatch.setattr(subject, "MCP", Mock(return_value=client))
    runtime = Mock()
    monkeypatch.setattr(subject, "_runtime", runtime)
    subject.authenticated_smoke(SHA)
    runtime.assert_called_once_with(client.client, subject.LOCAL_URL, SHA)
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


@pytest.fixture(name="target")
def code_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> subject.Deployer:
    target = deployer(tmp_path, monkeypatch)
    subject.LAUNCHER.write_text(launcher(tmp_path / "old"))
    target.runtime.mkdir(parents=True)
    monkeypatch.setattr(subject, "ENV_FILE", tmp_path / "env")
    subject.ENV_FILE.write_text("unchanged config")

    def git(path: Path, *arguments: str):
        sha = "b" * 40 if path == tmp_path / "old" else SHA
        outputs = {"rev-parse": sha, "ls-tree": "same dependencies"}
        return subject.subprocess.CompletedProcess([], 0, outputs.get(arguments[0], ""), "")

    monkeypatch.setattr(subject, "git", git)
    monkeypatch.setattr(subject, "command", Mock(return_value=Mock(stdout="schema")))
    monkeypatch.setattr(
        subject, "ScriptDirectory", Mock(return_value=Mock(get_heads=lambda: ["schema"]))
    )
    monkeypatch.setattr(target, "service", Mock(return_value=Mock(stdout="0")))
    monkeypatch.setattr(target, "backup", Mock(side_effect=AssertionError))
    monkeypatch.setattr(target, "migrate", Mock(side_effect=AssertionError))
    monkeypatch.setattr(target, "local_smoke", Mock())
    monkeypatch.setattr(subject, "authenticated_smoke", Mock())
    return target


@pytest.mark.parametrize("failure", [True, False])
def test_code_only_preserves_writes_and_supported_rollback(
    target: subject.Deployer, monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    old = subject.LAUNCHER.read_bytes()
    writes = ["before"]

    def service(*arguments: str):
        if arguments == ("restart",) and subject.LAUNCHER.read_bytes() != old:
            assert target.cutover_started
            writes.append("candidate write")
            if failure:
                raise RuntimeError("after launcher replacement")
        return Mock(stdout="0")

    monkeypatch.setattr(target, "service", service)
    monkeypatch.setattr(target, "smoke", Mock())
    if failure:
        with pytest.raises(RuntimeError):
            target.run("b" * 40)
    else:
        target.run("b" * 40)
        monkeypatch.setattr(subject, "Deployer", Mock(return_value=target))
        assert subject.main([SHA, "--repo", str(target.repo), "--rollback-code-only"]) == 0
        subject.authenticated_smoke.assert_called_once_with("b" * 40)  # type: ignore[attr-defined]
    assert subject.LAUNCHER.read_bytes() == old
    assert writes == ["before", "candidate write"]
    assert all(
        "psql" in call.args[0] or "--verify-import-only" in call.args[0]
        for call in subject.command.call_args_list
    )  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "drift", ["preimage", "active", "record", "config", "schema", "source", "launcher", "full"]
)
def test_code_only_rejects_drift_before_effects(
    target: subject.Deployer, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    target.prepare()
    target.code_record.write_text(target.code_identity())
    if drift != "preimage":
        subject.LAUNCHER.write_bytes(target.launcher_candidate.read_bytes())
    if drift == "preimage":
        target.code_record.unlink()
        target.launcher_before.write_text("retained preimage")
    elif drift == "record":
        target.code_record.unlink()
    elif drift == "config":
        subject.ENV_FILE.write_text("changed")
    elif drift == "schema":
        monkeypatch.setattr(subject, "command", Mock(return_value=Mock(stdout="other schema")))
    elif drift == "source":
        monkeypatch.setattr(subject, "git", Mock(return_value=Mock(stdout="dirty")))
    elif drift == "launcher":
        target.launcher_candidate.write_text("different candidate")
    before = target.launcher_before.read_bytes()
    current = subject.LAUNCHER.read_bytes()
    with pytest.raises((RuntimeError, OSError, SyntaxError)):
        if drift in ("preimage", "active", "full"):
            target.run(None if drift == "full" else "b" * 40)
        else:
            target.rollback_code_only()
    assert target.launcher_before.read_bytes() == before
    assert subject.LAUNCHER.read_bytes() == current
    target.service.assert_not_called()  # type: ignore[attr-defined]
