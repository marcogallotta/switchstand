"""Official Codex role entries share one home and select instructions by cwd."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _fixture(tmp_path: Path):
    home = tmp_path / "user home"
    auth = home / ".codex/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{}")
    legacy = home / ".local/state/switchstand/codex/coordinator"
    legacy.mkdir(parents=True)
    (legacy / "config.toml").write_text('model = "gpt-6-sol"\n')
    (legacy / "hooks.json").write_text('{"hooks":{"PreToolUse":[]}}')
    nvm = home / ".nvm"
    (nvm / "alias").mkdir(parents=True)
    (nvm / "alias/default").write_text("v24.16.0")
    (nvm / "nvm.sh").write_text(
        'nvm() { [[ "$1" == which && "$2" == default ]] || return 1; '
        'printf "%s\\n" "$HOME/.nvm/versions/node/$(cat "$HOME/.nvm/alias/default")/bin/node"; }\n'
    )
    bin_dir = nvm / "versions/node/v24.16.0/bin"
    bin_dir.mkdir(parents=True)
    cli = bin_dir / "codex"
    cli.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\n"
        "print(json.dumps({'cwd':os.getcwd(),'home':os.environ['CODEX_HOME'],"
        "'argv':sys.argv[1:]}))\n"
    )
    cli.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("NVM_DIR", None)
    env.pop("CODEX_HOME", None)
    return home, legacy, cli, env


def _run(role: str, env: dict, *args: str):
    return subprocess.run(
        [str(ROOT / "scripts" / f"launch-{role}"), *args],
        cwd="/tmp", env=env, text=True, capture_output=True, check=False,
    )


def test_worker_coordinator_worker_share_home_but_keep_role_cwd(tmp_path: Path):
    home, legacy, _, env = _fixture(tmp_path)
    shared = home / ".local/state/switchstand/codex/switchstand"
    for role in ("worker", "coordinator", "worker"):
        result = _run(role, env, "resume")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "cwd": str(ROOT / "roles" / role),
            "home": str(shared), "argv": ["resume"],
        }
        assert "../.." in (ROOT / "roles" / role / "AGENTS.md").read_text()
    assert (shared / ".switchstand-home").read_text() == "switchstand\n"
    assert (shared / "auth.json").resolve() == home / ".codex/auth.json"
    assert (shared / "config.toml").read_text() == 'model = "gpt-6-sol"\n'
    assert not (shared / "AGENTS.md").exists()
    assert not (shared / "hooks.json").exists()
    assert (legacy / "hooks.json").is_file()


def test_exact_legacy_recovery_and_old_home_preservation(tmp_path: Path):
    home, legacy, _, env = _fixture(tmp_path)
    old_worker = home / ".local/state/switchstand/codex/role-homes/worker"
    old_worker.mkdir(parents=True)
    (old_worker / "old-session").write_text("preserve")
    result = subprocess.run(
        [str(ROOT / "scripts/launch-legacy-codex"), "resume", "exact-id"],
        env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "cwd": str(ROOT), "home": str(legacy), "argv": ["resume", "exact-id"],
    }
    assert (old_worker / "old-session").read_text() == "preserve"
    assert (legacy / "config.toml").read_text() == 'model = "gpt-6-sol"\n'


def test_dynamic_nvm_default_and_raw_codex_unintercepted(tmp_path: Path):
    home, _, _, env = _fixture(tmp_path)
    second = home / ".nvm/versions/node/v24.17.0/bin"
    second.mkdir(parents=True)
    cli = second / "codex"
    cli.write_text("#!/bin/sh\necho new-version\n")
    cli.chmod(0o755)
    (home / ".nvm/alias/default").write_text("v24.17.0")
    wrapper = tmp_path / "bin"
    wrapper.mkdir()
    (wrapper / "codex").write_text("#!/bin/sh\necho wrong-wrapper\n")
    (wrapper / "codex").chmod(0o755)
    env["PATH"] = f"{wrapper}:{env['PATH']}"
    result = _run("worker", env, "--version")
    assert result.returncode == 0
    assert result.stdout.strip() == "new-version"
    raw = subprocess.run(["codex", "--version"], env=env, text=True, capture_output=True, check=False)
    assert raw.stdout.strip() == "wrong-wrapper"


@pytest.mark.parametrize("entry", ["AGENTS.md", "auth.json", "config.toml", "hooks.json"])
def test_existing_unexpected_home_is_not_overwritten(tmp_path: Path, entry: str):
    _, _, _, env = _fixture(tmp_path)
    shared = tmp_path / "existing"
    shared.mkdir()
    (shared / entry).write_text("preserve")
    env["CODEX_HOME"] = str(shared)
    result = _run("worker", env, "--version")
    assert result.returncode != 0
    assert (shared / entry).read_text() == "preserve"
    assert "Unexpected" in result.stderr


def test_concurrent_first_launch_uses_one_complete_home(tmp_path: Path):
    home, _, _, env = _fixture(tmp_path)
    processes = [subprocess.Popen(
        [str(ROOT / "scripts" / f"launch-{role}"), "--version"], env=env,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) for role in ("worker", "coordinator") * 6]
    results = [p.communicate(timeout=15) for p in processes]
    assert all(p.returncode == 0 for p in processes), results
    shared = home / ".local/state/switchstand/codex/switchstand"
    assert (shared / ".switchstand-home").read_text() == "switchstand\n"
    assert not list(shared.parent.glob(".codex-home.*"))
