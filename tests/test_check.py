import os
import stat
import subprocess
from pathlib import Path


def executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def test_focused_check_budgets_bootstrap_inside_stop_loss(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    scripts = repo / "scripts"
    scripts.mkdir()
    source = Path(__file__).parents[1] / "scripts" / "check"
    check = scripts / "check"
    check.write_bytes(source.read_bytes())
    check.chmod(0o755)
    executable(scripts / "bootstrap", "#!/bin/sh\nsleep 300\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    timeout_log = tmp_path / "timeout.log"
    executable(
        fake_bin / "timeout",
        """#!/bin/sh
printf '%s\n' "$*" >> "$TIMEOUT_LOG"
exit 124
""",
    )
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TIMEOUT_LOG": str(timeout_log),
    }
    result = subprocess.run(
        [check, "tests/test_one.py"],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 124
    first = timeout_log.read_text().splitlines()[0]
    seconds, command = first.split(" ", 1)
    assert 1 <= int(seconds) <= 120
    assert command == str(scripts / "bootstrap")
    assert "120-second stop-loss" in result.stderr
