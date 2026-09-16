import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


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


def real_start_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    committed_repo(source)
    scripts = source / "scripts"
    scripts.mkdir()
    start_source = Path(__file__).parents[1] / "scripts" / "switchstand-start"
    start = scripts / "switchstand-start"
    start.write_bytes(start_source.read_bytes())
    start.chmod(0o755)
    isolated_source = Path(__file__).parents[1] / "scripts" / "switchstand-isolated-launch"
    isolated = scripts / "switchstand-isolated-launch"
    isolated.write_bytes(isolated_source.read_bytes())
    isolated.chmod(0o755)
    executable(scripts / "bootstrap", "#!/bin/sh\nexit 17\n")
    run_git(
        source,
        "add",
        "scripts/switchstand-start",
        "scripts/switchstand-isolated-launch",
        "scripts/bootstrap",
    )
    run_git(source, "commit", "-m", "launcher")
    clone = tmp_path / "clone"
    clone_repo(source, clone)
    return source, clone, clone / "scripts" / "switchstand-start"


@pytest.mark.skipif(shutil.which("git") is None, reason="real Git executable required")
def test_start_fast_forwards_clean_main_to_exact_fetched_commit(tmp_path):
    source, clone, start = real_start_repo(tmp_path)
    before = run_git(clone, "rev-parse", "HEAD").stdout.strip()
    (source / "tracked.txt").write_text("new accepted revision\n")
    run_git(source, "add", "tracked.txt")
    run_git(source, "commit", "-m", "advance main")
    accepted = run_git(source, "rev-parse", "HEAD").stdout.strip()

    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith("SWITCHSTAND_CONTROL_")}
    result = subprocess.run(
        [start, "--active", "9999999999999999", "--commit", accepted],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 17, result.stderr
    assert before != accepted
    assert run_git(clone, "rev-parse", "HEAD").stdout.strip() == accepted
    assert run_git(clone, "status", "--porcelain", "--untracked-files=all").stdout == ""
    assert (clone / "tracked.txt").read_text() == "new accepted revision\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="real Git executable required")
@pytest.mark.parametrize("problem", ["dirty", "divergent"])
def test_start_preserves_dirty_or_divergent_main(tmp_path, problem):
    source, clone, start = real_start_repo(tmp_path)
    (source / "tracked.txt").write_text("remote advancement\n")
    run_git(source, "add", "tracked.txt")
    run_git(source, "commit", "-m", "advance main")
    before = run_git(clone, "rev-parse", "HEAD").stdout.strip()
    if problem == "dirty":
        (clone / "local.txt").write_text("uncommitted local work\n")
    else:
        run_git(clone, "config", "user.name", "Switchstand Test")
        run_git(clone, "config", "user.email", "switchstand-test@example.invalid")
        (clone / "local.txt").write_text("local committed work\n")
        run_git(clone, "add", "local.txt")
        run_git(clone, "commit", "-m", "local advancement")
        before = run_git(clone, "rev-parse", "HEAD").stdout.strip()

    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith("SWITCHSTAND_CONTROL_")}
    result = subprocess.run(
        [start, "--active", "9999999999999999", "--commit", "a" * 40],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert problem in result.stderr
    assert run_git(clone, "rev-parse", "HEAD").stdout.strip() == before
    assert (clone / "local.txt").read_text() == (
        "uncommitted local work\n" if problem == "dirty" else "local committed work\n"
    )
    assert (clone / "tracked.txt").read_text() == "accepted\n"


def fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    fake_bin = tmp_path / "bin"
    home = tmp_path / "home"
    state_root = home / ".local" / "state" / "switchstand" / "worktrees"
    state_root.mkdir(parents=True, mode=0o700)
    state_root.chmod(0o700)
    target = state_root / "switchstand-task-9999999999999999"
    common = repo / ".git"
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
    isolated_source = Path(__file__).parents[1] / "scripts" / "switchstand-isolated-launch"
    isolated = scripts / "switchstand-isolated-launch"
    isolated.write_bytes(isolated_source.read_bytes())
    isolated.chmod(0o755)
    executable(fake_bin / "git", """#!/bin/sh
case "$*" in
  *"fetch --quiet --no-tags origin"*) : ;;
  *"rev-parse --show-toplevel") echo "$FAKE_TARGET" ;;
  *"rev-parse --path-format=absolute --git-common-dir") echo "$FAKE_COMMON" ;;
  *"rev-parse HEAD") [ "$2" = "$FAKE_TARGET" ] && echo "$FAKE_TARGET_HEAD" || echo "$FAKE_HEAD" ;;
  *"branch --show-current") printf '%s\\n' "$FAKE_BRANCH" ;;
  *"rev-parse origin/main") echo "$FAKE_ACCEPTED" ;;
  *"status --porcelain"*)
      if [ "$2" = "$FAKE_TARGET" ]; then
          [ "$FAKE_TARGET_DIRTY" = 0 ] || echo dirty
      else
          [ "$FAKE_CONTROL_DIRTY" = 0 ] || echo dirty
      fi
      ;;
  *"cat-file -t"*) echo "$FAKE_OBJECT_TYPE" ;;
  *"merge-base --is-ancestor"*) : ;;
  *"merge --ff-only --quiet"*) : ;;
  *"worktree list --porcelain"*) printf 'worktree %s\\n\\n' "$FAKE_TARGET" ;;
  *) exit 91 ;;
esac
""")
    executable(control_python, """#!/bin/sh
if [ "$1" = "-P" ] && [ "$2" = "-c" ]; then
  printf '%s\\n' "$*" > "$FAKE_TASK_REF_LOG"
  echo 9999999999999999
  exit 0
fi
if [ "$1" = "-P" ] && [ "$2" = "-m" ] && [ "$3" = "switchstand.launch_source" ]; then
  printf '%s\\n' "$*" > "$FAKE_SOURCE_ARGS"
  printf '%s\\n' "${ASANA_TOKEN:-}" > "$FAKE_SOURCE_TOKEN"
  if [ "${FAKE_SOURCE_FAIL:-0}" = 1 ]; then
    echo 'launch source preparation failed: simulated exact source failure' >&2
    exit 1
  fi
  printf '%s %s\\n' "$FAKE_ACCEPTED" "$FAKE_CANDIDATE"
  exit 0
fi
pwd > "$FAKE_LAUNCH_CWD"
printf '%s\\n' "${ASANA_TOKEN:-}" > "$FAKE_LAUNCH_TOKEN"
printf '%s\n' "$@" > "$FAKE_LAUNCH_ARGS"
printf '%s\n' "$SWITCHSTAND_REQUESTING_GIT_COMMON" > "$FAKE_LAUNCH_COMMON"
printf '%s\n' "$SWITCHSTAND_CONTROL_ROOT" > "$FAKE_CONTROL_ROOT"
printf '%s\n' "$SWITCHSTAND_CANDIDATE_ROOT" > "$FAKE_CANDIDATE_ROOT"
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
        "HOME": str(home),
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
        "FAKE_OBJECT_TYPE": "commit",
        "FAKE_TARGET": str(target),
        "FAKE_TASK_REF_LOG": str(tmp_path / "task-ref.log"),
        "FAKE_SOURCE_ARGS": str(tmp_path / "source.args"),
        "FAKE_SOURCE_TOKEN": str(tmp_path / "source.token"),
        "FAKE_WORKTREE_LOG": str(tmp_path / "worktree.log"),
        "FAKE_WORKTREE_CWD": str(tmp_path / "worktree.cwd"),
        "FAKE_LAUNCH_CWD": str(tmp_path / "launch.cwd"),
        "FAKE_LAUNCH_TOKEN": str(tmp_path / "launch.token"),
        "FAKE_LAUNCH_ARGS": str(tmp_path / "launch.args"),
        "FAKE_LAUNCH_COMMON": str(tmp_path / "launch.common"),
        "FAKE_CONTROL_ROOT": str(tmp_path / "control.root"),
        "FAKE_CANDIDATE_ROOT": str(tmp_path / "candidate.root"),
        "FAKE_BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
    }
    return start, environment, target


def test_start_creates_task_writer_and_forwards_launch_arguments(tmp_path):
    start, environment, target = fixture(tmp_path)
    environment["ASANA_TOKEN"] = "ambient-token-must-not-reach-candidate"
    result = subprocess.run(
        [
            start,
            "--active",
            "9999999999999999",
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
    source_args = (
        f"-P -m switchstand.launch_source --repo {start.parents[1]} "
        f"--control-sha {'a' * 40} 9999999999999999"
    )
    assert (tmp_path / "source.args").read_text().splitlines() == [source_args]
    assert (tmp_path / "worktree.log").read_text() == (
        f"task-9999999999999999 {'a' * 40} --resume-exact\n"
    )
    assert (tmp_path / "worktree.cwd").read_text().strip() == str(start.parents[1])
    assert (tmp_path / "launch.cwd").read_text().strip() == str(start.parents[1])
    assert (tmp_path / "launch.args").read_text().splitlines() == [
        "-P", "-m", "switchstand.launch", "--active", "9999999999999999",
        "--commit", "a" * 40, "--reference", "42", "do work",
    ]
    assert (tmp_path / "launch.common").read_text().strip() == environment["FAKE_COMMON"]
    assert (tmp_path / "control.root").read_text().strip() == str(start.parents[1])
    assert (tmp_path / "candidate.root").read_text().strip() == str(target)
    assert (tmp_path / "source.token").read_text() == "\n"
    assert (tmp_path / "launch.token").read_text() == "\n"


@pytest.mark.parametrize(
    ("problem", "expected"),
    [
        ("incomplete-selector", "selector CONTROL identity is incomplete"),
        ("wrong-path", "does not match selector identity"),
        ("wrong-head", "does not match selector identity"),
        ("wrong-common", "does not match selector identity"),
        ("attached-branch", "does not match selector identity"),
        ("dirty-control", "does not match selector identity"),
    ],
)
def test_start_requires_exact_selector_control_before_task_read(tmp_path, problem, expected):
    start, environment, _ = fixture(tmp_path)
    if problem == "incomplete-selector":
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
        [start, "--active", "9999999999999999", "--commit", "a" * 40],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert expected in result.stderr
    assert not (tmp_path / "task-ref.log").exists()
    assert not (tmp_path / "source.args").exists()
    assert not (tmp_path / "worktree.log").exists()
    assert not (tmp_path / "bootstrap.log").exists()


def test_start_direct_fallback_requires_exact_fast_forward_readback(tmp_path):
    start, environment, _ = fixture(tmp_path)
    environment.pop("SWITCHSTAND_CONTROL_PATH")
    environment.pop("SWITCHSTAND_CONTROL_SHA")
    environment.pop("SWITCHSTAND_CONTROL_COMMON")
    environment["FAKE_BRANCH"] = "main"
    environment["FAKE_ACCEPTED"] = "b" * 40
    result = subprocess.run(
        [start, "--active=9999999999999999", f"--commit={'a' * 40}"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "read back clean main at the exact fetched revision" in result.stderr
    assert not (tmp_path / "task-ref.log").exists()
    assert not (tmp_path / "source.args").exists()
    assert not (tmp_path / "worktree.log").exists()


def test_start_stops_when_exact_task_source_resolution_fails(tmp_path):
    start, environment, _ = fixture(tmp_path)
    environment["FAKE_SOURCE_FAIL"] = "1"
    result = subprocess.run(
        [start, "--active=9999999999999999", f"--commit={'a' * 40}"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "simulated exact source failure" in result.stderr
    assert not (tmp_path / "worktree.log").exists()
    assert (tmp_path / "bootstrap.log").exists()


def test_start_rejects_task_candidate_that_differs_from_requested_commit(tmp_path):
    start, environment, _ = fixture(tmp_path)
    environment["FAKE_CANDIDATE"] = "b" * 40
    result = subprocess.run(
        [start, "--active=9999999999999999", f"--commit={'a' * 40}"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "candidate does not match requested --commit" in result.stderr
    assert not (tmp_path / "worktree.log").exists()
    assert (tmp_path / "bootstrap.log").exists()


def test_start_refuses_inexact_candidate_without_launching(tmp_path):
    start, environment, _ = fixture(tmp_path)
    environment["FAKE_TARGET_HEAD"] = "b" * 40
    result = subprocess.run(
        [start, "--active", "9999999999999999", "--commit", "a" * 40],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "registered worktree at the exact requested checkpoint" in result.stderr
    assert not (tmp_path / "launch.args").exists()
    assert (tmp_path / "bootstrap.log").exists()


def test_start_keeps_legacy_task_writer_intact_without_creating_second_writer(tmp_path):
    start, environment, _ = fixture(tmp_path)
    legacy = tmp_path / "old-temp" / "switchstand-task-9999999999999999"
    legacy.mkdir(parents=True)
    (legacy / "unfinished.txt").write_text("keep me\n")
    environment["TMPDIR"] = str(legacy.parent)
    blocked = subprocess.run(
        [start, "--active", "9999999999999999", "--commit", "a" * 40],
        env=environment, text=True, capture_output=True, check=False,
    )
    assert blocked.returncode == 1
    assert "legacy task writer exists" in blocked.stderr
    assert (legacy / "unfinished.txt").read_text() == "keep me\n"
    assert not (tmp_path / "launch.args").exists()
    assert not (tmp_path / "worktree.log").exists()


def test_start_rejects_relative_home_before_creating_task_writer(tmp_path):
    start, environment, _ = fixture(tmp_path)
    environment["HOME"] = "relative-home"
    result = subprocess.run(
        [start, "--active", "9999999999999999", "--commit", "a" * 40],
        env=environment, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 1
    assert "HOME must be absolute" in result.stderr
    assert not (tmp_path / "worktree.log").exists()


def test_start_uses_canonical_durable_target_for_noncanonical_home(tmp_path):
    start, environment, target = fixture(tmp_path)
    environment["HOME"] = str(tmp_path / "home" / ".." / "home")
    result = subprocess.run(
        [start, "--active", "9999999999999999", "--commit", "a" * 40],
        env=environment, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "candidate.root").read_text().strip() == str(target)


@pytest.mark.parametrize("commit", ["a" * 39, "A" * 40, "main"])
def test_start_requires_exact_lowercase_commit(tmp_path, commit):
    start, environment, _ = fixture(tmp_path)
    result = subprocess.run(
        [start, "--active", "9999999999999999", "--commit", commit],
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
        "TMPDIR": "/",
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
    assert result.stdout == f"{tmp_path / '.local/state/switchstand/worktrees/switchstand-sample'}\n"
    assert Path(result.stdout.strip()).parent.stat().st_mode & 0o777 == 0o700
    assert "HEAD is now at accepted" in result.stderr


@pytest.mark.parametrize(
    "problem",
    [None, "wrong-root", "wrong-branch", "changed-baseline", "dirty", "divergent"],
)
def test_worktree_helper_reuses_only_valid_clean_task_writer(tmp_path, problem):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    fake_bin = tmp_path / "bin"
    target = tmp_path / ".local/state/switchstand/worktrees/switchstand-sample"
    git_dir = tmp_path / "linked-git-dir"
    common = tmp_path / "common"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    target.mkdir(parents=True)
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
        assert "not a registered linked worktree" in result.stderr


@pytest.mark.skipif(shutil.which("git") is None, reason="real Git executable required")
def test_worktree_helper_rejects_foreign_clone_collision_and_preserves_it(tmp_path):
    source_repo = tmp_path / "source"
    sha = committed_repo(source_repo)
    requester = tmp_path / "requester"
    foreign = tmp_path / "foreign"
    clone_repo(source_repo, requester)
    clone_repo(source_repo, foreign)

    target = tmp_path / ".local/state/switchstand/worktrees/switchstand-sample"
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

    target = tmp_path / ".local/state/switchstand/worktrees/switchstand-sample"
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


@pytest.mark.skipif(shutil.which("git") is None, reason="real Git executable required")
@pytest.mark.parametrize("moved_head", [False, True])
def test_task_writer_relaunch_preserves_dirty_exact_checkpoint(tmp_path, moved_head):
    source = tmp_path / "source"
    checkpoint = committed_repo(source)
    requester = tmp_path / "requester"
    clone_repo(source, requester)
    target = tmp_path / ".local/state/switchstand/worktrees/switchstand-task-123"
    run_git(requester, "worktree", "add", "-b", "v2-task-123", str(target), checkpoint)
    git_dir = Path(run_git(target, "rev-parse", "--absolute-git-dir").stdout.strip())
    marker = git_dir / "switchstand-green-sha"
    marker.write_text(checkpoint + "\n")
    (target / "tracked.txt").write_text("unfinished edit\n")
    (target / "new.txt").write_text("unfinished new file\n")
    if moved_head:
        run_git(target, "config", "user.name", "Switchstand Test")
        run_git(target, "config", "user.email", "switchstand-test@example.invalid")
        run_git(target, "add", "tracked.txt")
        run_git(target, "commit", "-m", "local checkpoint moved")
        (target / "tracked.txt").write_text("unfinished edit\n")
    before_head = run_git(target, "rev-parse", "HEAD").stdout.strip()
    before_status = run_git(target, "status", "--porcelain", "--untracked-files=all").stdout
    before_registry = run_git(requester, "worktree", "list", "--porcelain").stdout

    helper = Path(__file__).parents[1] / "scripts" / "switchstand-worktree"
    result = subprocess.run(
        [helper, "task-123", checkpoint, "--resume-exact"], cwd=requester,
        env=os.environ | {"TMPDIR": str(tmp_path)},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == (1 if moved_head else 0), result.stderr
    if not moved_head:
        assert result.stdout == str(target) + "\n"
    else:
        assert "exact requested checkpoint" in result.stderr
    assert run_git(target, "rev-parse", "HEAD").stdout.strip() == before_head
    assert run_git(target, "status", "--porcelain", "--untracked-files=all").stdout == before_status
    assert run_git(requester, "worktree", "list", "--porcelain").stdout == before_registry
    assert (target / "tracked.txt").read_text() == "unfinished edit\n"
    assert (target / "new.txt").read_text() == "unfinished new file\n"
    assert marker.read_text() == checkpoint + "\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="real Git executable required")
def test_task_writer_reuses_newer_pushed_checkpoint_without_rewriting_green(tmp_path):
    source = tmp_path / "source"
    green = committed_repo(source)
    requester = tmp_path / "requester"
    clone_repo(source, requester)
    target = tmp_path / ".local/state/switchstand/worktrees/switchstand-task-123"
    run_git(requester, "worktree", "add", "-b", "v2-task-123", str(target), green)
    git_dir = Path(run_git(target, "rev-parse", "--absolute-git-dir").stdout.strip())
    marker = git_dir / "switchstand-green-sha"
    marker.write_text(green + "\n")
    run_git(target, "config", "user.name", "Switchstand Test")
    run_git(target, "config", "user.email", "switchstand-test@example.invalid")
    (target / "tracked.txt").write_text("pushed checkpoint\n")
    run_git(target, "add", "tracked.txt")
    run_git(target, "commit", "-m", "next checkpoint")
    checkpoint = run_git(target, "rev-parse", "HEAD").stdout.strip()
    run_git(target, "push", "origin", "HEAD:refs/heads/candidate")
    (target / "new.txt").write_text("unfinished after checkpoint\n")
    before_status = run_git(target, "status", "--porcelain", "--untracked-files=all").stdout

    result = subprocess.run(
        [Path(__file__).parents[1] / "scripts" / "switchstand-worktree",
         "task-123", checkpoint, "--resume-exact"],
        cwd=requester, env=os.environ | {"TMPDIR": str(tmp_path)},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(target) + "\n"
    assert run_git(target, "rev-parse", "HEAD").stdout.strip() == checkpoint
    assert run_git(target, "status", "--porcelain", "--untracked-files=all").stdout == before_status
    assert marker.read_text() == green + "\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="real Git executable required")
def test_work_writer_relaunch_preserves_dirty_and_committed_progress(tmp_path):
    source = tmp_path / "source"
    green = committed_repo(source)
    requester = tmp_path / "requester"
    clone_repo(source, requester)
    helper = Path(__file__).parents[1] / "scripts" / "switchstand-worktree"
    environment = os.environ | {"HOME": str(tmp_path)}
    target = tmp_path / ".local/state/switchstand/worktrees/switchstand-work-123"

    created = subprocess.run(
        [helper, "work-123", green, "--resume-work"], cwd=requester, env=environment,
        text=True, capture_output=True, check=False,
    )
    assert created.returncode == 0, created.stderr
    assert created.stdout == str(target) + "\n"
    assert f"worktree {target}\n" in run_git(requester, "worktree", "list", "--porcelain").stdout

    (target / "unfinished.txt").write_text("dirty progress\n")
    run_git(requester, "config", "user.name", "Switchstand Test")
    run_git(requester, "config", "user.email", "switchstand-test@example.invalid")
    (requester / "main-progress.txt").write_text("new main\n")
    run_git(requester, "add", "main-progress.txt")
    run_git(requester, "commit", "-m", "advance main")
    newer_main = run_git(requester, "rev-parse", "HEAD").stdout.strip()

    dirty_relaunch = subprocess.run(
        [helper, "work-123", newer_main, "--resume-work"], cwd=requester, env=environment,
        text=True, capture_output=True, check=False,
    )
    assert dirty_relaunch.returncode == 0, dirty_relaunch.stderr
    assert dirty_relaunch.stdout == str(target) + "\n"
    assert (target / "unfinished.txt").read_text() == "dirty progress\n"

    run_git(target, "config", "user.name", "Switchstand Test")
    run_git(target, "config", "user.email", "switchstand-test@example.invalid")
    run_git(target, "add", "unfinished.txt")
    run_git(target, "commit", "-m", "task checkpoint")
    task_head = run_git(target, "rev-parse", "HEAD").stdout.strip()
    (target / "after-checkpoint.txt").write_text("still dirty\n")

    committed_relaunch = subprocess.run(
        [helper, "work-123", newer_main, "--resume-work"], cwd=requester, env=environment,
        text=True, capture_output=True, check=False,
    )
    assert committed_relaunch.returncode == 0, committed_relaunch.stderr
    assert run_git(target, "rev-parse", "HEAD").stdout.strip() == task_head
    assert (target / "after-checkpoint.txt").read_text() == "still dirty\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="real Git executable required")
def test_task_writer_canonicalizes_noncanonical_state_root_before_create_and_reuse(tmp_path):
    source = tmp_path / "source"
    checkpoint = committed_repo(source)
    requester = tmp_path / "requester"
    clone_repo(source, requester)
    state = tmp_path / ".local/state/switchstand/worktrees"
    state.mkdir(parents=True, mode=0o700)
    alias = tmp_path / "state-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    target = state / "switchstand-task-123"
    helper = Path(__file__).parents[1] / "scripts" / "switchstand-worktree"

    created = subprocess.run(
        [helper, "task-123", checkpoint, "--resume-exact"],
        cwd=requester, env=os.environ | {"HOME": str(alias)},
        text=True, capture_output=True, check=False,
    )
    assert created.returncode == 0, created.stderr
    assert created.stdout == str(target) + "\n"
    git_dir = Path(run_git(target, "rev-parse", "--absolute-git-dir").stdout.strip())
    (git_dir / "switchstand-green-sha").write_text(checkpoint + "\n")
    (target / "unfinished.txt").write_text("keep me\n")
    before = run_git(target, "status", "--porcelain", "--untracked-files=all").stdout

    reused = subprocess.run(
        [helper, "task-123", checkpoint, "--resume-exact"],
        cwd=requester, env=os.environ | {"HOME": str(tmp_path / "ignored" / "..")},
        text=True, capture_output=True, check=False,
    )
    assert reused.returncode == 0, reused.stderr
    assert reused.stdout == str(target) + "\n"
    assert run_git(target, "status", "--porcelain", "--untracked-files=all").stdout == before
    assert (target / "unfinished.txt").read_text() == "keep me\n"


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
