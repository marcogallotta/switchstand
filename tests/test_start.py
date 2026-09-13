import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


def executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=True,
    )


def committed_repo(path: Path) -> str:
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        text=True,
        capture_output=True,
        check=True,
    )
    run_git(path, "config", "user.name", "Switchstand Test")
    run_git(path, "config", "user.email", "switchstand-test@example.invalid")
    (path / "tracked.txt").write_text("accepted\n")
    run_git(path, "add", "tracked.txt")
    run_git(path, "commit", "-m", "accepted")
    return run_git(path, "rev-parse", "HEAD").stdout.strip()


def clone_repo(source: Path, target: Path) -> None:
    subprocess.run(
        ["git", "clone", str(source), str(target)],
        text=True,
        capture_output=True,
        check=True,
    )


def fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    fake_bin = tmp_path / "bin"
    target = tmp_path / "writer"
    common = tmp_path / "shared" / ".git"
    control_python = repo / ".venv" / "bin" / "python"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    (target / "scripts").mkdir(parents=True)
    control_python.parent.mkdir(parents=True)
    common.mkdir(parents=True)
    source = Path(__file__).parents[1] / "scripts" / "switchstand-start"
    start = scripts / "switchstand-start"
    start.write_bytes(source.read_bytes())
    start.chmod(0o755)
    executable(fake_bin / "git", """#!/bin/sh
case "$*" in
  *"rev-parse --show-toplevel") echo "$FAKE_TARGET" ;;
  *"rev-parse --path-format=absolute --git-common-dir") echo "$FAKE_COMMON" ;;
  *"rev-parse HEAD") [ "$2" = "$FAKE_TARGET" ] && echo "$FAKE_TARGET_HEAD" || echo "$FAKE_HEAD" ;;
  *"branch --show-current") printf '%s\\n' "$FAKE_BRANCH" ;;
  *"status --porcelain"*)
      if [ "$2" = "$FAKE_TARGET" ]; then
          [ "$FAKE_TARGET_DIRTY" = 0 ] || echo dirty
      else
          [ "$FAKE_CONTROL_DIRTY" = 0 ] || echo dirty
      fi
      ;;
  *"worktree list --porcelain"*) printf 'worktree %s\\n\\n' "$FAKE_TARGET" ;;
  *) exit 91 ;;
esac
""")
    executable(fake_bin / "python3", """#!/bin/sh
if [ "$1" = "-P" ] && [ "$2" = "-c" ]; then
    echo 1218383014436992
    exit 0
fi
if [ "$1" = "-P" ] && [ "$2" = "-m" ] && [ "$3" = "switchstand.launch_source" ]; then
    printf '%s\\n' "$*" > "$FAKE_SOURCE_ARGS"
    if [ "${FAKE_SOURCE_FAIL:-0}" = 1 ]; then
        echo 'launch source preparation failed: simulated exact source failure' >&2
        exit 1
    fi
    printf '%s %s\\n' "$FAKE_ACCEPTED" "$FAKE_CANDIDATE"
    exit 0
fi
exit 92
""")
    executable(control_python, """#!/bin/sh
pwd > "$FAKE_LAUNCH_CWD"
printf '%s\\n' "$@" > "$FAKE_LAUNCH_ARGS"
printf '%s\\n' "$PYTHONPATH" > "$FAKE_LAUNCH_PYTHONPATH"
printf '%s\\n' "$SWITCHSTAND_REQUESTING_GIT_COMMON" > "$FAKE_LAUNCH_COMMON"
""")
    executable(
        scripts / "bootstrap",
        "#!/bin/sh\nprintf 'called\\n' > \"$FAKE_BOOTSTRAP_LOG\"\n",
    )
    executable(scripts / "switchstand-worktree", """#!/bin/sh
printf '%s\\n' "$*" > "$FAKE_WORKTREE_LOG"
pwd > "$FAKE_WORKTREE_CWD"
echo 'git progress belongs on stderr' >&2
echo "$FAKE_TARGET"
""")
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SWITCHSTAND_CONTROL_PATH": str(repo),
        "SWITCHSTAND_CONTROL_SHA": "a" * 40,
        "SWITCHSTAND_CONTROL_COMMON": str(common),
        "FAKE_HEAD": "a" * 40,
        "FAKE_BRANCH": "",
        "FAKE_COMMON": str(common),
        "FAKE_CONTROL_DIRTY": "0",
        "FAKE_ACCEPTED": "a" * 40,
        "FAKE_CANDIDATE": "a" * 40,
        "FAKE_TARGET_HEAD": "a" * 40,
        "FAKE_TARGET_DIRTY": "0",
        "FAKE_TARGET": str(target),
        "FAKE_SOURCE_ARGS": str(tmp_path / "source.args"),
        "FAKE_WORKTREE_LOG": str(tmp_path / "worktree.log"),
        "FAKE_WORKTREE_CWD": str(tmp_path / "worktree.cwd"),
        "FAKE_LAUNCH_CWD": str(tmp_path / "launch.cwd"),
        "FAKE_LAUNCH_ARGS": str(tmp_path / "launch.args"),
        "FAKE_LAUNCH_PYTHONPATH": str(tmp_path / "launch.pythonpath"),
        "FAKE_LAUNCH_COMMON": str(tmp_path / "launch.common"),
        "FAKE_BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
    }
    return start, environment, target


def test_start_creates_task_writer_and_forwards_launch_arguments(tmp_path):
    start, environment, target = fixture(tmp_path)
    result = subprocess.run(
        [
            start,
            "--active",
            "1218383014436992",
            "--commit",
            "a" * 40,
            "--reference",
            "42",
            "do work",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "source.args").read_text().splitlines() == [
        f"-P -m switchstand.launch_source --repo {start.parents[1]} "
        f"--control-sha {'a' * 40} 1218383014436992"
    ]
    assert (tmp_path / "worktree.log").read_text() == (
        f"task-1218383014436992 {'a' * 40}\n"
    )
    assert (tmp_path / "worktree.cwd").read_text().strip() == str(start.parents[1])
    assert (tmp_path / "launch.cwd").read_text().strip() == str(target)
    assert (tmp_path / "launch.pythonpath").read_text().strip() == str(
        start.parents[1] / "src"
    )
    assert (tmp_path / "launch.args").read_text().splitlines() == [
        "-P",
        "-m",
        "switchstand.launch",
        "--active",
        "1218383014436992",
        "--commit",
        "a" * 40,
        "--reference",
        "42",
        "do work",
    ]
    assert (tmp_path / "launch.common").read_text().strip() == environment["FAKE_COMMON"]
    assert (tmp_path / "bootstrap.log").read_text() == "called\n"


@pytest.mark.parametrize(
    ("problem", "expected"),
    [
        ("missing-selector", "selector-provided exact CONTROL identity"),
        ("wrong-path", "selector-provided CONTROL path"),
        ("wrong-head", "does not match the selector-provided identity"),
        ("wrong-common", "does not match the selector-provided identity"),
        ("attached-branch", "does not match the selector-provided identity"),
        ("dirty-control", "does not match the selector-provided identity"),
    ],
)
def test_start_requires_exact_selector_control_before_task_read(tmp_path, problem, expected):
    start, environment, _ = fixture(tmp_path)
    if problem == "missing-selector":
        environment.pop("SWITCHSTAND_CONTROL_SHA")
    elif problem == "wrong-path":
        environment["SWITCHSTAND_CONTROL_PATH"] = str(tmp_path / "other")
    elif problem == "wrong-head":
        environment["SWITCHSTAND_CONTROL_SHA"] = "b" * 40
    elif problem == "wrong-common":
        environment["SWITCHSTAND_CONTROL_COMMON"] = str(tmp_path / "other-common")
    elif problem == "attached-branch":
        environment["FAKE_BRANCH"] = "main"
    else:
        environment["FAKE_CONTROL_DIRTY"] = "1"

    result = subprocess.run(
        [start, "--active", "1218383014436992", "--commit", "a" * 40],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert expected in result.stderr
    assert not (tmp_path / "source.args").exists()
    assert not (tmp_path / "worktree.log").exists()
    assert not (tmp_path / "bootstrap.log").exists()


def test_start_stops_when_exact_task_source_resolution_fails(tmp_path):
    start, environment, _ = fixture(tmp_path)
    environment["FAKE_SOURCE_FAIL"] = "1"
    result = subprocess.run(
        [start, "--active=1218383014436992", f"--commit={'a' * 40}"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "simulated exact source failure" in result.stderr
    assert not (tmp_path / "worktree.log").exists()
    assert not (tmp_path / "bootstrap.log").exists()


def test_start_rejects_task_candidate_that_differs_from_requested_commit(tmp_path):
    start, environment, _ = fixture(tmp_path)
    environment["FAKE_CANDIDATE"] = "b" * 40
    result = subprocess.run(
        [start, "--active=1218383014436992", f"--commit={'a' * 40}"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "candidate does not match requested --commit" in result.stderr
    assert not (tmp_path / "worktree.log").exists()
    assert not (tmp_path / "bootstrap.log").exists()


@pytest.mark.parametrize("problem", ["wrong-head", "dirty"])
def test_start_refuses_inexact_candidate_without_launching(tmp_path, problem):
    start, environment, _ = fixture(tmp_path)
    if problem == "wrong-head":
        environment["FAKE_TARGET_HEAD"] = "b" * 40
    else:
        environment["FAKE_TARGET_DIRTY"] = "1"
    result = subprocess.run(
        [start, "--active", "1218383014436992", "--commit", "a" * 40],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "registered clean worktree at the exact requested commit" in result.stderr
    assert not (tmp_path / "launch.args").exists()
    assert not (tmp_path / "bootstrap.log").exists()


@pytest.mark.parametrize("commit", ["a" * 39, "A" * 40, "main"])
def test_start_requires_exact_lowercase_commit(tmp_path, commit):
    start, environment, _ = fixture(tmp_path)
    result = subprocess.run(
        [start, "--active", "1218383014436992", "--commit", commit],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert not (tmp_path / "source.args").exists()
    assert not (tmp_path / "worktree.log").exists()


def test_worktree_helper_keeps_git_progress_off_stdout(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    fake_bin = tmp_path / "bin"
    git_dir = tmp_path / "git-dir"
    common = tmp_path / "common"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    git_dir.mkdir()
    common.mkdir()
    source = Path(__file__).parents[1] / "scripts" / "switchstand-worktree"
    helper = scripts / "switchstand-worktree"
    helper.write_bytes(source.read_bytes())
    helper.chmod(0o755)
    executable(fake_bin / "git", """#!/bin/sh
case "$*" in
  "rev-parse --show-toplevel") echo "$FAKE_REPO" ;;
  *"rev-parse --path-format=absolute --git-common-dir") echo "$FAKE_COMMON" ;;
  *"cat-file -e"*) : ;;
  *"show-ref --verify --quiet"*) exit 1 ;;
  *"worktree add"*) echo 'HEAD is now at accepted' ;;
  *"rev-parse --absolute-git-dir") echo "$FAKE_GIT_DIR" ;;
  *) exit 91 ;;
esac
""")
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "FAKE_REPO": str(repo),
        "FAKE_COMMON": str(common),
        "FAKE_GIT_DIR": str(git_dir),
    }
    result = subprocess.run(
        [helper, "sample", "a" * 40],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{tmp_path / 'switchstand-sample'}\n"
    assert "HEAD is now at accepted" in result.stderr


@pytest.mark.parametrize(
    "problem",
    [None, "wrong-root", "wrong-branch", "changed-baseline", "dirty", "divergent"],
)
def test_worktree_helper_reuses_only_valid_clean_task_writer(tmp_path, problem):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    fake_bin = tmp_path / "bin"
    target = tmp_path / "switchstand-sample"
    git_dir = tmp_path / "linked-git-dir"
    common = tmp_path / "common"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    target.mkdir()
    git_dir.mkdir()
    common.mkdir()
    sha, current = "a" * 40, "b" * 40
    recorded = "c" * 40 if problem == "changed-baseline" else sha
    (git_dir / "switchstand-green-sha").write_text(recorded + "\n")
    source = Path(__file__).parents[1] / "scripts" / "switchstand-worktree"
    helper = scripts / "switchstand-worktree"
    helper.write_bytes(source.read_bytes())
    helper.chmod(0o755)
    executable(fake_bin / "git", """#!/bin/sh
case "$*" in
  "rev-parse --show-toplevel") echo "$FAKE_REPO" ;;
  *"rev-parse --path-format=absolute --git-common-dir") echo "$FAKE_COMMON" ;;
  *"cat-file -e"*) : ;;
  *"show-ref --verify --quiet"*) : ;;
  *"rev-parse --show-toplevel"*) echo "$FAKE_ACTUAL_TARGET" ;;
  *"branch --show-current"*) echo "$FAKE_BRANCH" ;;
  *"rev-parse HEAD"*) echo "$FAKE_SHA" ;;
  *"rev-parse --absolute-git-dir"*) echo "$FAKE_GIT_DIR" ;;
  *"status --porcelain"*) [ "$FAKE_DIRTY" = 0 ] || echo dirty ;;
  *"merge-base --is-ancestor"*) exit "$FAKE_DIVERGENT" ;;
  *"worktree list --porcelain"*) printf 'worktree %s\n\n' "$FAKE_REGISTERED_TARGET" ;;
  *) exit 91 ;;
esac
""")
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "FAKE_REPO": str(repo),
        "FAKE_ACTUAL_TARGET": str(tmp_path / "wrong" if problem == "wrong-root" else target),
        "FAKE_BRANCH": "wrong" if problem == "wrong-branch" else "v2-sample",
        "FAKE_SHA": current,
        "FAKE_GIT_DIR": str(git_dir),
        "FAKE_COMMON": str(common),
        "FAKE_REGISTERED_TARGET": str(target),
        "FAKE_DIRTY": "1" if problem == "dirty" else "0",
        "FAKE_DIVERGENT": "1" if problem == "divergent" else "0",
    }
    result = subprocess.run(
        [helper, "sample", sha],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if problem is None:
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"{target}\n"
    else:
        assert result.returncode == 1
        assert "not a clean registered linked worktree" in result.stderr


@pytest.mark.skipif(shutil.which("git") is None, reason="real Git executable required")
def test_worktree_helper_rejects_foreign_clone_collision_and_preserves_it(tmp_path):
    source_repo = tmp_path / "source"
    sha = committed_repo(source_repo)
    requester = tmp_path / "requester"
    foreign = tmp_path / "foreign"
    clone_repo(source_repo, requester)
    clone_repo(source_repo, foreign)

    target = tmp_path / "switchstand-sample"
    run_git(requester, "branch", "v2-sample", sha)
    run_git(foreign, "worktree", "add", "-b", "v2-sample", str(target), sha)
    target_git_dir = Path(
        run_git(target, "rev-parse", "--absolute-git-dir").stdout.strip()
    )
    marker = target_git_dir / "switchstand-green-sha"
    marker.write_text(sha + "\n")

    before_git_file = (target / ".git").read_text()
    before_registry = run_git(foreign, "worktree", "list", "--porcelain").stdout
    helper = Path(__file__).parents[1] / "scripts" / "switchstand-worktree"
    environment = os.environ | {"TMPDIR": str(tmp_path)}

    result = subprocess.run(
        [helper, "sample", sha],
        cwd=requester,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "registered linked worktree of the requesting repository" in result.stderr
    assert target.exists()
    assert (target / ".git").read_text() == before_git_file
    assert run_git(target, "rev-parse", "HEAD").stdout.strip() == sha
    assert run_git(target, "status", "--porcelain").stdout == ""
    assert marker.read_text() == sha + "\n"
    assert run_git(foreign, "worktree", "list", "--porcelain").stdout == before_registry


@pytest.mark.skipif(shutil.which("git") is None, reason="real Git executable required")
def test_worktree_helper_reuses_registered_requesting_repo_worktree(tmp_path):
    source_repo = tmp_path / "source"
    sha = committed_repo(source_repo)
    requester = tmp_path / "requester"
    clone_repo(source_repo, requester)

    target = tmp_path / "switchstand-sample"
    run_git(requester, "worktree", "add", "-b", "v2-sample", str(target), sha)
    target_git_dir = Path(
        run_git(target, "rev-parse", "--absolute-git-dir").stdout.strip()
    )
    (target_git_dir / "switchstand-green-sha").write_text(sha + "\n")

    helper = Path(__file__).parents[1] / "scripts" / "switchstand-worktree"
    environment = os.environ | {"TMPDIR": str(tmp_path)}
    result = subprocess.run(
        [helper, "sample", sha],
        cwd=requester,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{target}\n"
    assert (
        run_git(target, "rev-parse", "--path-format=absolute", "--git-common-dir")
        .stdout.strip()
        == run_git(
            requester, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).stdout.strip()
    )
    assert f"worktree {target}\n" in run_git(
        requester, "worktree", "list", "--porcelain"
    ).stdout


def test_launch_uses_shared_project_environment(tmp_path):
    repo = tmp_path / "writer"
    scripts = repo / "scripts"
    fake_bin = tmp_path / "bin"
    common = tmp_path / "shared" / ".git"
    python = tmp_path / "shared" / ".venv" / "bin" / "python"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    common.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    source = Path(__file__).parents[1] / "scripts" / "switchstand-launch"
    launch = scripts / "switchstand-launch"
    launch.write_bytes(source.read_bytes())
    launch.chmod(0o755)
    executable(fake_bin / "git", """#!/bin/sh
case "$*" in
  *"rev-parse --absolute-git-dir") echo "$FAKE_GIT_DIR" ;;
  *"rev-parse --path-format=absolute --git-common-dir") echo "$FAKE_COMMON" ;;
  *) exit 91 ;;
esac
""")
    executable(python, """#!/bin/sh
printf '%s\n' "$PYTHONPATH" > "$FAKE_PYTHONPATH"
printf '%s\n' "$@" > "$FAKE_PYTHON_ARGS"
printf '%s\n' "$SWITCHSTAND_REQUESTING_GIT_COMMON" > "$FAKE_LAUNCH_COMMON"
""")
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_GIT_DIR": str(tmp_path / "linked-git-dir"),
        "FAKE_COMMON": str(common),
        "FAKE_PYTHONPATH": str(tmp_path / "pythonpath"),
        "FAKE_PYTHON_ARGS": str(tmp_path / "python.args"),
        "FAKE_LAUNCH_COMMON": str(tmp_path / "launch.common"),
    }
    result = subprocess.run(
        [launch, "--active", "123"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "pythonpath").read_text().strip().split(":")[0] == str(repo / "src")
    assert (tmp_path / "python.args").read_text().splitlines() == [
        "-m",
        "switchstand.launch",
        "--active",
        "123",
    ]
    assert (tmp_path / "launch.common").read_text().strip() == str(common)
