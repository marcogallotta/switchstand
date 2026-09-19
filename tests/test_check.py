import os
import stat
import subprocess
from pathlib import Path

from test_check_environment import fixture

from switchstand.check_environment import prepare

ROOT = Path(__file__).parents[1]


def unbound_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items()
            if name not in {"SWITCHSTAND_CHECK_VENV", "SWITCHSTAND_CHECK_MANIFEST"}}


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
        repo / "src/switchstand/check_environment.py",
        "import os, pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, lambda *_: pathlib.Path(os.environ['FAKE_TERM']).touch())\n"
        "while True: time.sleep(0.1)\n",
    )
    executable(fake_bin / "docker", "#!/bin/sh\n: > \"$FAKE_DOCKER\"\n")
    environment = unbound_environment() | {
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
    assert not (tmp_path / "docker.ran").exists()
    assert not (tmp_path / "quality.log").exists()


def test_check_fails_actionably_when_timeout_is_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    fake_bin = tmp_path / "bin"
    check = copy_script("check", repo)
    fake_bin.mkdir()
    executable(fake_bin / "git", "#!/bin/sh\nexit 99\n")
    executable(fake_bin / "date", "#!/bin/sh\necho 1000\n")
    environment = unbound_environment() | {"PATH": str(fake_bin)}
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
    environment = unbound_environment() | {"PATH": str(fake_bin), "FAKE_REPO": str(repo)}
    result = subprocess.run(
        [bootstrap], cwd=repo, env=environment, text=True, capture_output=True, check=False
    )
    assert result.returncode == 1
    assert "bootstrap prerequisite 'docker' is missing" in result.stderr
    assert "make 'docker' available on PATH" in result.stderr
    assert "rerun scripts/bootstrap in the primary checkout" in result.stderr


def test_check_keeps_normal_focused_checks_valid(tmp_path: Path) -> None:
    repo, uv = fixture(tmp_path)
    check = copy_script("check", repo)
    helper = repo / "src/switchstand/check_environment.py"
    helper.parent.mkdir(parents=True)
    helper.write_bytes((ROOT / "src/switchstand/check_environment.py").read_bytes())
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    generation, _ = prepare(repo, uv)
    venv = Path(generation) / "bin"
    for tool in ("ruff", "pyright", "pytest"):
        executable(
            venv / tool,
            f"#!/bin/sh\nprintf '%s %s\\n' '{tool}' \"$*\" >> \"$FAKE_QUALITY\"\n",
        )
    environment = unbound_environment() | {"FAKE_QUALITY": str(tmp_path / "quality.log")}
    result = subprocess.run(
        [check, "tests/test_check.py"],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "quality.log").read_text().splitlines()
    assert calls[0] == "ruff check ."
    assert calls[1].startswith("pyright --pythonpath ")
    assert calls[2] == "pytest tests/test_check.py"

    executable(venv / "ruff", "#!/bin/sh\nexit 42\n")
    result = subprocess.run([check], cwd=repo, env=environment, capture_output=True, check=False)
    assert result.returncode == 42


def tree_bytes(root: Path) -> dict[str, bytes | str]:
    return {
        str(path.relative_to(root)): (
            os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else "directory"
        )
        for path in root.rglob("*")
    }


def test_bootstrap_verify_reuses_receipt_without_mutation(tmp_path: Path) -> None:
    repo = tmp_path / "primary"
    bootstrap = copy_script("bootstrap", repo)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "pyproject.toml").write_text("[project]\n")
    (repo / "uv.lock").write_text("lock\n")
    tools = repo / ".git/switchstand-tools"
    # A fake pinned uv lets the real bootstrap own receipt creation and freshness.
    executable(tools / "uv-0.12.10", "#!/bin/sh\nexit 0\n")
    for tool in ("python", "ruff", "pyright", "pytest"):
        executable(repo / ".venv/bin" / tool, "#!/bin/sh\nexit 0\n")
    setup = subprocess.run([bootstrap], capture_output=True, text=True, check=False)
    assert setup.returncode == 0, setup.stderr
    before = tree_bytes(repo)
    result = subprocess.run([bootstrap, "--verify-only"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    import hashlib

    expected = hashlib.sha256(b"[project]\nlock\n").hexdigest()
    assert result.stdout.splitlines() == [str(repo / ".venv"), expected]
    assert tree_bytes(repo) == before
    for failure in ("stale", "incomplete", "missing"):
        if failure == "stale":
            (repo / "uv.lock").write_text("changed\n")
        elif failure == "incomplete":
            (repo / "uv.lock").write_text("lock\n")
            (repo / ".venv/bin/pytest").unlink()
        else:
            for receipt in tools.glob("environment-*"):
                receipt.unlink()
        before = tree_bytes(repo)
        result = subprocess.run([bootstrap, "--verify-only"], capture_output=True, text=True, check=False)
        assert result.returncode == 1
        assert not result.stdout
        assert "run scripts/bootstrap in the primary checkout" in result.stderr
        assert tree_bytes(repo) == before


def test_bound_private_check_validates_binding_without_bootstrap(tmp_path: Path) -> None:
    repo, uv = fixture(tmp_path)
    check = copy_script("check", repo)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    helper = repo / "src/switchstand/check_environment.py"
    helper.parent.mkdir(parents=True)
    helper.write_bytes((ROOT / "src/switchstand/check_environment.py").read_bytes())
    executable(repo / "scripts/bootstrap", "#!/bin/sh\necho forbidden-bootstrap >&2\nexit 99\n")
    generation, _ = prepare(repo, uv)
    venv = Path(generation)
    log = tmp_path / "quality.log"
    for tool in ("ruff", "pyright", "pytest"):
        executable(venv / "bin" / tool, f"#!/bin/sh\necho {tool} >> '{log}'\n")
    environment = unbound_environment()
    # A stale launch binding must never select the shared environment.
    binding = {"SWITCHSTAND_CHECK_VENV": str(tmp_path / "primary/.venv"),
               "SWITCHSTAND_CHECK_MANIFEST": "a" * 64}
    result = subprocess.run([check], cwd=repo, env=environment | binding, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["ruff", "pyright", "pytest"]
    assert not (repo / ".venv").exists()
    for target in (repo / "uv.lock", venv / "complete.json"):
        original = target.read_bytes()
        executable(venv / "bin/ruff", f'#!/bin/sh\nprintf changed >> "{target}"\n')
        result = subprocess.run([check], cwd=repo, env=environment, capture_output=True, text=True, check=False)
        assert result.returncode != 0 and "changed" in result.stderr
        target.write_bytes(original)
    (venv / "bin/pytest").unlink()
    result = subprocess.run([check], cwd=repo, env=environment | binding, capture_output=True, text=True, check=False)
    assert result.returncode == 1 and "incomplete" in result.stderr
    assert "forbidden-bootstrap" not in result.stderr
