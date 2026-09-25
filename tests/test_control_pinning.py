import os
import stat
import subprocess
import tomllib
from pathlib import Path

from switchstand.codex_runtime import codex_command

ROOT = Path(__file__).parents[1]


def executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_managed_codex_and_mcp_wrappers_remain_control_rooted(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    hostile = candidate / "src" / "switchstand"
    hostile.mkdir(parents=True)
    marker = tmp_path / "candidate-executed"
    (hostile / "development.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('candidate')\n"
    )
    (candidate / ".codex").mkdir()
    (candidate / ".codex" / "config.toml").write_text("model = 'candidate-shadow'\n")

    command = codex_command(Path("/control"), candidate, [])
    assert command[1:5] == ["-C", "/control", "--add-dir", str(candidate)]
    config = tomllib.loads((ROOT / ".codex" / "config.toml").read_text())
    assert config["mcp_servers"]["switchstand_managed"]["command"] == (
        "scripts/switchstand-controller-mcp"
    )
    assert config["mcp_servers"]["switchstand_development"]["command"] == (
        "scripts/switchstand-development-mcp"
    )

    fake_bin = tmp_path / "bin"
    primary = tmp_path / "control-primary"
    common = primary / ".git"
    python = primary / ".venv" / "bin" / "python"
    fake_bin.mkdir()
    common.mkdir(parents=True)
    python.parent.mkdir(parents=True)

    dev_receipt = tmp_path / "development-receipt"
    executable(
        fake_bin / "git",
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *\"rev-parse --path-format=absolute --git-common-dir\"*) "
        "printf '%s\\n' \"$FAKE_COMMON\" ;;\n"
        "  *) exit 91 ;;\n"
        "esac\n",
    )
    executable(
        python,
        "#!/bin/sh\n"
        "printf '%s\\n%s\\n%s\\n' \"$0\" \"${PYTHONPATH:-}\" \"$*\" "
        "> \"$PIN_RECEIPT\"\n",
    )
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SWITCHSTAND_MANAGED": "1",
        "SWITCHSTAND_WORKTREE": str(candidate),
        "SWITCHSTAND_BRANCH": "candidate",
        "SWITCHSTAND_GIT_COMMON": str(common),
        "FAKE_COMMON": str(common),
        "PIN_RECEIPT": str(dev_receipt),
    }
    result = subprocess.run(
        [ROOT / "scripts" / "switchstand-development-mcp"],
        cwd=candidate,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    executable_path, pythonpath, arguments = dev_receipt.read_text().splitlines()
    assert Path(executable_path) == python
    assert pythonpath == str(ROOT / "src")
    assert arguments == "-m switchstand.development"

    docker_receipt = tmp_path / "controller-receipt"
    executable(
        fake_bin / "docker",
        "#!/bin/sh\nprintf '%s\\n%s\\n' \"$PWD\" \"$*\" > \"$DOCKER_RECEIPT\"\n",
    )
    env |= {
        "ACTIVE_WORK_ID": "00000000-0000-0000-0000-000000000001",
        "SWITCHSTAND_GIT_DIR": str(common / "worktrees" / "candidate"),
        "SWITCHSTAND_RUN_ID": "00000000-0000-0000-0000-000000000002",
        "DOCKER_RECEIPT": str(docker_receipt),
    }
    result = subprocess.run(
        [ROOT / "scripts" / "switchstand-controller-mcp"],
        cwd=candidate,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    docker_cwd, docker_args = docker_receipt.read_text().splitlines()
    assert docker_cwd == str(ROOT)
    assert f"-v {candidate}:{candidate}:ro" in docker_args
    assert not marker.exists()
