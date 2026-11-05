"""Role entries bypass the global wrapper and preserve the shared home."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("script", "expected_cwd"),
    [
        ("launch-worker", ROOT),
        ("launch-coordinator", ROOT / "roles" / "coordinator"),
    ],
)
@pytest.mark.parametrize("supplied_home", [False, True])
def test_explicit_role_launch_preserves_arguments_home_and_exit(
    tmp_path: Path, script: str, expected_cwd: Path, supplied_home: bool
) -> None:
    fake_home = tmp_path / "user home"
    official_bin = fake_home / ".nvm/versions/node/v24.16.0/bin"
    official_bin.mkdir(parents=True)
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
            "CODEX_HOME", str(fake_home / ".local/state/switchstand/codex/coordinator")
        ),
    }


@pytest.mark.parametrize("script", ["launch-worker", "launch-coordinator"])
def test_missing_official_cli_does_not_fall_back_to_path_wrapper(
    tmp_path: Path, script: str
) -> None:
    wrapper_bin = tmp_path / "bin"
    wrapper_bin.mkdir()
    wrapper = wrapper_bin / "codex"
    wrapper.write_text("#!/bin/sh\nexit 99\n")
    wrapper.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{wrapper_bin}:{env['PATH']}"

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
