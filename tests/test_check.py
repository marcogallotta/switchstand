import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def copy_script(name: str, repo: Path) -> Path:
    path = repo / "scripts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((ROOT / "scripts" / name).read_bytes())
    path.chmod(0o755)
    return path


def fake_git(path: Path) -> None:
    executable(
        path,
        """#!/bin/sh
case "$*" in
  "rev-parse --show-toplevel") echo "$FAKE_REPO" ;;
  *"rev-parse --path-format=absolute --git-common-dir") echo "$FAKE_REPO/.git" ;;
  *) exit 91 ;;
esac
""",
    )


def find_command(name: str) -> Path:
    for directory in os.environ["PATH"].split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise AssertionError(f"required test command not found: {name}")


def test_check_kills_term_resistant_bootstrap_inside_stop_loss(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    fake_bin = tmp_path / "bin"
    check = copy_script("check", repo)
    fake_bin.mkdir()
    fake_git(fake_bin / "git")
    executable(
        fake_bin / "date",
        """#!/bin/sh
if [ -f "$FAKE_DATE_STATE" ]; then
    echo 1118
else
    : > "$FAKE_DATE_STATE"
    echo 1000
fi
""",
    )
    executable(
        repo / "scripts" / "bootstrap",
        """#!/bin/sh
trap 'printf term > "$FAKE_TERM"' TERM
while :; do
    sleep 0.1
done
""",
    )
    executable(fake_bin / "docker", "#!/bin/sh\n: > \"$FAKE_DOCKER\"\n")
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_REPO": str(repo),
        "FAKE_DATE_STATE": str(tmp_path / "date.state"),
        "FAKE_TERM": str(tmp_path / "term.seen"),
        "FAKE_DOCKER": str(tmp_path / "docker.ran"),
    }
    result = subprocess.run(
        [check],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=4,
    )
    assert result.returncode == 124
    assert (tmp_path / "term.seen").exists()
    assert "setup exceeded the 120-second stop-loss" in result.stderr
    assert "run scripts/bootstrap in the primary checkout" in result.stderr
    assert not (tmp_path / "docker.ran").exists()
    assert not (tmp_path / "quality.log").exists()


def test_check_fails_actionably_when_timeout_is_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    fake_bin = tmp_path / "bin"
    check = copy_script("check", repo)
    fake_bin.mkdir()
    executable(fake_bin / "git", "#!/bin/sh\nexit 99\n")
    executable(fake_bin / "date", "#!/bin/sh\necho 1000\n")
    environment = os.environ | {"PATH": str(fake_bin)}
    result = subprocess.run(
        [check], cwd=repo, env=environment, text=True, capture_output=True, check=False
    )
    assert result.returncode == 1
    assert "prerequisite 'timeout' is missing" in result.stderr
    assert "make 'timeout' available on PATH, then rerun scripts/check" in result.stderr


def test_bootstrap_fails_actionably_when_docker_is_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    fake_bin = tmp_path / "bin"
    bootstrap = copy_script("bootstrap", repo)
    fake_bin.mkdir()
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text("[project]\n")
    (repo / "uv.lock").write_text("lock\n")
    fake_git(fake_bin / "git")
    for command_name in ("dirname", "sha256sum", "cut", "mkdir", "chmod", "mv", "rm"):
        (fake_bin / command_name).symlink_to(find_command(command_name))
    executable(fake_bin / "python3", "#!/bin/sh\necho 'Python 3.14.0'\n")
    environment = os.environ | {"PATH": str(fake_bin), "FAKE_REPO": str(repo)}
    result = subprocess.run(
        [bootstrap], cwd=repo, env=environment, text=True, capture_output=True, check=False
    )
    assert result.returncode == 1
    assert "bootstrap prerequisite 'docker' is missing" in result.stderr
    assert "make 'docker' available on PATH" in result.stderr
    assert "rerun scripts/bootstrap in the primary checkout" in result.stderr


def test_check_keeps_normal_focused_checks_valid(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    fake_bin = tmp_path / "bin"
    check = copy_script("check", repo)
    fake_bin.mkdir()
    fake_git(fake_bin / "git")
    executable(repo / "scripts" / "bootstrap", "#!/bin/sh\n: > \"$FAKE_BOOTSTRAP\"\n")
    venv = repo / ".venv" / "bin"
    executable(venv / "python", "#!/bin/sh\necho manifest\n")
    for tool in ("ruff", "pyright", "pytest"):
        executable(
            venv / tool,
            f"#!/bin/sh\nprintf '%s %s\\n' '{tool}' \"$*\" >> \"$FAKE_QUALITY\"\n",
        )
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_REPO": str(repo),
        "FAKE_BOOTSTRAP": str(tmp_path / "bootstrap.ran"),
        "FAKE_QUALITY": str(tmp_path / "quality.log"),
    }
    result = subprocess.run(
        [check, "tests/test_check.py"],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "bootstrap.ran").exists()
    calls = (tmp_path / "quality.log").read_text().splitlines()
    assert calls[0] == "ruff check ."
    assert calls[1].startswith("pyright --pythonpath ")
    assert calls[2] == "pytest tests/test_check.py"
