"""The explicit role entries select a directory without changing Codex settings."""

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
def test_explicit_role_launch_preserves_arguments_home_and_exit(
    tmp_path: Path, script: str, expected_cwd: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "print(json.dumps({'cwd': os.getcwd(), 'argv': sys.argv[1:], "
        "'home': os.environ.get('CODEX_HOME')}))\n"
        "sys.exit(37)\n"
    )
    fake_codex.chmod(0o755)

    caller = tmp_path / "unrelated caller"
    caller.mkdir()
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
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
        "home": env["CODEX_HOME"],
    }
