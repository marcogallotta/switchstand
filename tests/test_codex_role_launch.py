"""Role entries use isolated Codex homes and the official CLI."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _fake_nvm(home: Path, version: str = "v24.16.0") -> Path:
    auth = home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text("{}")
    legacy = home / ".local/state/switchstand/codex/coordinator"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "config.toml").write_text('model = "gpt-6-sol"\n')
    (legacy / "hooks.json").write_text('{"hooks": {"PreToolUse": []}}')
    nvm_dir = home / ".nvm"
    (nvm_dir / "alias").mkdir(parents=True, exist_ok=True)
    (nvm_dir / "alias" / "default").write_text(version)
    (nvm_dir / "nvm.sh").write_text(
        'nvm() {\n'
        '  [[ "$1" == which && "$2" == default ]] || return 1\n'
        '  printf "%s\\n" "$HOME/.nvm/versions/node/$(cat "$HOME/.nvm/alias/default")/bin/node"\n'
        '}\n'
    )
    node_bin = nvm_dir / "versions" / "node" / version / "bin"
    node_bin.mkdir(parents=True)
    return node_bin


@pytest.mark.parametrize(
    ("script", "expected_cwd"),
    [
        ("launch-worker", ROOT),
        ("launch-coordinator", ROOT),
    ],
)
@pytest.mark.parametrize("supplied_home", [False, True])
def test_explicit_role_launch_preserves_arguments_home_and_exit(
    tmp_path: Path, script: str, expected_cwd: Path, supplied_home: bool
) -> None:
    fake_home = tmp_path / "user home"
    official_bin = _fake_nvm(fake_home)
    official_codex = official_bin / "codex"
    official_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "print(json.dumps({'cwd': os.getcwd(), 'argv': sys.argv[1:], "
        "'home': os.environ.get('CODEX_HOME')}))\n"
        "sys.exit(37)\n"
    )
    official_codex.chmod(0o755)
    wrapper_bin = tmp_path / "wrapper on PATH"
    wrapper_bin.mkdir()
    wrapper = wrapper_bin / "codex"
    wrapper.write_text("#!/bin/sh\nexit 99\n")
    wrapper.chmod(0o755)

    caller = tmp_path / "unrelated caller"
    caller.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["PATH"] = f"{wrapper_bin}:{env['PATH']}"
    env.pop("NVM_DIR", None)
    env.pop("CODEX_HOME", None)
    if supplied_home:
        env["CODEX_HOME"] = str(tmp_path / "shared codex home")

    result = subprocess.run(
        [str(ROOT / "scripts" / script), "exec", "two words", "--", "-c", "x=y"],
        cwd=caller,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 37
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "cwd": str(expected_cwd),
        "argv": ["exec", "two words", "--", "-c", "x=y"],
        "home": env.get(
            "CODEX_HOME", str(fake_home / ".local/state/switchstand/codex/role-homes" / script.removeprefix("launch-"))
        ),
    }
    role_home = Path(json.loads(result.stdout)["home"])
    assert (role_home / "AGENTS.md").is_symlink()
    assert (role_home / "AGENTS.md").resolve() == ROOT / "roles" / script.removeprefix("launch-") / "AGENTS.md"
    assert (role_home / "auth.json").is_symlink()
    assert (role_home / "auth.json").resolve() == fake_home / ".codex" / "auth.json"
    assert (role_home / "config.toml").read_text() == 'model = "gpt-6-sol"\n'
    if script == "launch-coordinator":
        assert (role_home / "hooks.json").is_file()
    else:
        assert not (role_home / "hooks.json").exists()


@pytest.mark.parametrize("script", ["launch-worker", "launch-coordinator"])
def test_missing_official_cli_does_not_fall_back_to_path_wrapper(
    tmp_path: Path, script: str
) -> None:
    _fake_nvm(tmp_path)
    wrapper_bin = tmp_path / "bin"
    wrapper_bin.mkdir()
    wrapper = wrapper_bin / "codex"
    wrapper.write_text("#!/bin/sh\nexit 99\n")
    wrapper.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{wrapper_bin}:{env['PATH']}"
    env.pop("NVM_DIR", None)
    env.pop("CODEX_HOME", None)

    result = subprocess.run(
        [str(ROOT / "scripts" / script), "resume", "session-id"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 127
    assert "Official Codex CLI is missing" in result.stderr


@pytest.mark.parametrize("script", ["launch-worker", "launch-coordinator"])
def test_role_launch_follows_nvm_default_upgrade_and_passes_resume_without_id(
    tmp_path: Path, script: str
) -> None:
    older = _fake_nvm(tmp_path)
    newer = _fake_nvm(tmp_path, "v24.17.0")
    for node_bin, marker in ((older, "older"), (newer, "newer")):
        cli = node_bin / "codex"
        cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"print(json.dumps({{'installation': '{marker}', 'argv': sys.argv[1:]}}))\n"
        )
        cli.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env.pop("NVM_DIR", None)
    env.pop("CODEX_HOME", None)
    expected = {
        "installation": "newer",
        "argv": ["resume"],
    }
    result = subprocess.run(
        [str(ROOT / "scripts" / script), "resume"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == expected

    (tmp_path / ".nvm" / "alias" / "default").write_text("v24.16.0")
    previous = subprocess.run(
        [str(ROOT / "scripts" / script), "resume"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert previous.returncode == 0
    assert json.loads(previous.stdout) == {"installation": "older", "argv": ["resume"]}


@pytest.mark.parametrize("entry", ["AGENTS.md", "auth.json", "config.toml", "hooks.json"])
def test_role_home_rejects_unexpected_existing_entries(tmp_path: Path, entry: str) -> None:
    node_bin = _fake_nvm(tmp_path)
    cli = node_bin / "codex"
    cli.write_text("#!/bin/sh\necho should-not-run\n")
    cli.chmod(0o755)
    role_home = tmp_path / "role-home"
    role_home.mkdir()
    (role_home / entry).write_text("unexpected")
    env = os.environ.copy()
    env.update(HOME=str(tmp_path), CODEX_HOME=str(role_home))
    env.pop("NVM_DIR", None)

    result = subprocess.run(
        [str(ROOT / "scripts" / "launch-worker"), "--version"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "Unexpected" in result.stderr
    assert "should-not-run" not in result.stdout


def test_worker_config_is_one_time_copy_and_legacy_launcher_keeps_old_home(tmp_path: Path) -> None:
    node_bin = _fake_nvm(tmp_path)
    cli = node_bin / "codex"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "print(json.dumps({'home': os.environ['CODEX_HOME'], 'args': sys.argv[1:]}))\n"
    )
    cli.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env.pop("NVM_DIR", None)
    env.pop("CODEX_HOME", None)
    worker = tmp_path / ".local/state/switchstand/codex/role-homes/worker"
    legacy = tmp_path / ".local/state/switchstand/codex/coordinator"

    first = subprocess.run([str(ROOT / "scripts/launch-worker"), "resume"], env=env,
                           capture_output=True, text=True, check=False)
    assert first.returncode == 0
    (legacy / "config.toml").write_text('model = "changed"\n')
    second = subprocess.run([str(ROOT / "scripts/launch-worker"), "resume"], env=env,
                            capture_output=True, text=True, check=False)
    assert second.returncode == 0
    assert (worker / "config.toml").read_text() == 'model = "gpt-6-sol"\n'
    assert not (worker / "hooks.json").exists()

    recovered = subprocess.run(
        [str(ROOT / "scripts/launch-legacy-codex"), "resume", "exact-id"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert recovered.returncode == 0
    assert json.loads(recovered.stdout) == {"home": str(legacy), "args": ["resume", "exact-id"]}
