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


def init_repo(path: Path) -> str:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("base\n")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "base")
    return git(path, "rev-parse", "HEAD")


def fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    fake_bin = tmp_path / "bin"
    target = tmp_path / "writer"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    (target / "scripts").mkdir(parents=True)
    source = Path(__file__).parents[1] / "scripts" / "switchstand-start"
    start = scripts / "switchstand-start"
    start.write_bytes(source.read_bytes())
    start.chmod(0o755)
    executable(
        fake_bin / "git",
        """#!/bin/sh
case "$*" in
  *"branch --show-current") echo main ;;
  *"rev-parse HEAD") echo "$FAKE_HEAD" ;;
  *"rev-parse origin/main") echo "$FAKE_ACCEPTED" ;;
  *"status --porcelain") : ;;
  *"show-ref --verify --quiet"*) exit 1 ;;
  *) exit 91 ;;
esac
""",
    )
    executable(fake_bin / "python3", "#!/bin/sh\necho 1218383014436992\n")
    executable(scripts / "bootstrap", "#!/bin/sh\n:\n")
    executable(
        scripts / "switchstand-worktree",
        """#!/bin/sh
printf '%s\n' "$*" > "$FAKE_WORKTREE_LOG"
pwd > "$FAKE_WORKTREE_CWD"
echo 'git progress belongs on stderr' >&2
echo "$FAKE_TARGET"
""",
    )
    executable(
        target / "scripts" / "switchstand-launch",
        """#!/bin/sh
pwd > "$FAKE_LAUNCH_CWD"
printf '%s\n' "$@" > "$FAKE_LAUNCH_ARGS"
""",
    )
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_HEAD": "a" * 40,
        "FAKE_ACCEPTED": "a" * 40,
        "FAKE_TARGET": str(target),
        "FAKE_WORKTREE_LOG": str(tmp_path / "worktree.log"),
        "FAKE_WORKTREE_CWD": str(tmp_path / "worktree.cwd"),
        "FAKE_LAUNCH_CWD": str(tmp_path / "launch.cwd"),
        "FAKE_LAUNCH_ARGS": str(tmp_path / "launch.args"),
    }
    return start, environment, target


def test_start_creates_task_writer_and_forwards_launch_arguments(tmp_path):
    start, environment, target = fixture(tmp_path)
    result = subprocess.run(
        [start, "--active", "1218383014436992", "--reference", "42", "do work"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "worktree.log").read_text() == (
        f"task-1218383014436992 {'a' * 40}\n"
    )
    assert (tmp_path / "worktree.cwd").read_text().strip() == str(start.parents[1])
    assert (tmp_path / "launch.cwd").read_text().strip() == str(target)
    assert (tmp_path / "launch.args").read_text().splitlines() == [
        "--active",
        "1218383014436992",
        "--reference",
        "42",
        "do work",
    ]


def test_start_rejects_a_main_that_is_not_the_accepted_commit(tmp_path):
    start, environment, _ = fixture(tmp_path)
    environment["FAKE_ACCEPTED"] = "b" * 40
    result = subprocess.run(
        [start, "--active=1218383014436992"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "clean main at the locally accepted origin/main" in result.stderr
    assert not (tmp_path / "worktree.log").exists()


def test_worktree_helper_keeps_git_progress_off_stdout(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    fake_bin = tmp_path / "bin"
    git_dir = tmp_path / "git-dir"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    git_dir.mkdir()
    source = Path(__file__).parents[1] / "scripts" / "switchstand-worktree"
    helper = scripts / "switchstand-worktree"
    helper.write_bytes(source.read_bytes())
    helper.chmod(0o755)
    executable(
        fake_bin / "git",
        """#!/bin/sh
case "$*" in
  "rev-parse --show-toplevel") echo "$FAKE_REPO" ;;
  *"rev-parse --path-format=absolute --git-common-dir"*) echo "$FAKE_COMMON" ;;
  *"cat-file -e"*) : ;;
  *"show-ref --verify --quiet"*) exit 1 ;;
  *"worktree add"*) echo 'HEAD is now at accepted' ;;
  *"rev-parse --absolute-git-dir") echo "$FAKE_GIT_DIR" ;;
  *) exit 91 ;;
esac
""",
    )
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "FAKE_REPO": str(repo),
        "FAKE_COMMON": str(repo / ".git"),
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


def test_worktree_helper_reuses_only_exact_clean_owned_task_writer(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    fake_bin = tmp_path / "bin"
    target = tmp_path / "switchstand-sample"
    git_dir = tmp_path / "linked-git-dir"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    target.mkdir()
    git_dir.mkdir()
    sha = "a" * 40
    (git_dir / "switchstand-green-sha").write_text(sha + "\n")
    source = Path(__file__).parents[1] / "scripts" / "switchstand-worktree"
    helper = scripts / "switchstand-worktree"
    helper.write_bytes(source.read_bytes())
    helper.chmod(0o755)
    executable(
        fake_bin / "git",
        """#!/bin/sh
case "$*" in
  "rev-parse --show-toplevel") echo "$FAKE_REPO" ;;
  *"cat-file -e"*) : ;;
  *"show-ref --verify --quiet"*) : ;;
  *"worktree list --porcelain"*) echo "worktree $FAKE_TARGET" ;;
  *"rev-parse --show-toplevel"*) echo "$FAKE_TARGET" ;;
  *"branch --show-current"*) echo v2-sample ;;
  *"rev-parse HEAD"*) echo "$FAKE_SHA" ;;
  *"rev-parse --absolute-git-dir"*) echo "$FAKE_GIT_DIR" ;;
  *"rev-parse --path-format=absolute --git-common-dir"*) echo "$FAKE_COMMON" ;;
  *"status --porcelain"*) : ;;
  *) exit 91 ;;
esac
""",
    )
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "FAKE_REPO": str(repo),
        "FAKE_TARGET": str(target),
        "FAKE_SHA": sha,
        "FAKE_GIT_DIR": str(git_dir),
        "FAKE_COMMON": str(repo / ".git"),
    }
    result = subprocess.run(
        [helper, "sample", sha],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{target}\n"


def test_worktree_helper_rejects_same_name_writer_from_other_clone(tmp_path):
    source_repo = tmp_path / "source"
    sha = init_repo(source_repo)
    other_repo = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", str(source_repo), str(other_repo)],
        check=True,
        capture_output=True,
    )
    git(other_repo, "config", "user.email", "test@example.com")
    git(other_repo, "config", "user.name", "Test")
    git(source_repo, "branch", "v2-sample", sha)
    shared_tmp = tmp_path / "writers"
    shared_tmp.mkdir()
    helper = Path(__file__).parents[1] / "scripts" / "switchstand-worktree"
    environment = os.environ | {"TMPDIR": str(shared_tmp)}
    created = subprocess.run(
        [helper, "sample", sha],
        cwd=other_repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    collision = subprocess.run(
        [helper, "sample", sha],
        cwd=source_repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert collision.returncode == 1
    assert "owned requested baseline" in collision.stderr


def make_start_repo(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    repo = tmp_path / "start-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    scripts = repo / "scripts"
    scripts.mkdir()
    for name in ("switchstand-start", "switchstand-worktree"):
        source = Path(__file__).parents[1] / "scripts" / name
        target = scripts / name
        target.write_bytes(source.read_bytes())
        target.chmod(0o755)
    executable(scripts / "bootstrap", "#!/bin/sh\n:\n")
    executable(
        scripts / "switchstand-launch",
        """#!/bin/sh
if [ "${DIRTY_LAUNCH:-}" = 1 ]; then
    printf 'user work\n' > user-change.txt
fi
exit 1
""",
    )
    (repo / "README.md").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", sha)
    fake_bin = tmp_path / "start-bin"
    fake_bin.mkdir()
    executable(fake_bin / "python3", "#!/bin/sh\necho 123\n")
    writers = tmp_path / "start-writers"
    writers.mkdir()
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TMPDIR": str(writers),
    }
    return repo, writers, environment


def test_failed_clean_launch_removes_owned_writer_and_allows_new_baseline_retry(tmp_path):
    repo, writers, environment = make_start_repo(tmp_path)
    start = repo / "scripts" / "switchstand-start"
    target = writers / "switchstand-task-123"
    branch = "v2-task-123"
    first = subprocess.run(
        [start, "--active", "123"],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first.returncode == 1
    assert not target.exists()
    assert subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
    ).returncode == 1
    (repo / "README.md").write_text("advanced\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "advance")
    advanced = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", advanced)
    second = subprocess.run(
        [start, "--active", "123"],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert second.returncode == 1
    assert not target.exists()
    assert "cleaned untouched failed-launch writer" in second.stderr


def test_failed_dirty_launch_preserves_owned_writer_and_user_work(tmp_path):
    repo, writers, environment = make_start_repo(tmp_path)
    environment["DIRTY_LAUNCH"] = "1"
    start = repo / "scripts" / "switchstand-start"
    target = writers / "switchstand-task-123"
    result = subprocess.run(
        [start, "--active", "123"],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert target.is_dir()
    assert (target / "user-change.txt").read_text() == "user work\n"
    assert "preserving failed-launch writer" in result.stderr


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
    executable(
        fake_bin / "git",
        """#!/bin/sh
case "$*" in
  *"rev-parse --absolute-git-dir") echo "$FAKE_GIT_DIR" ;;
  *"rev-parse --path-format=absolute --git-common-dir") echo "$FAKE_COMMON" ;;
  *) exit 91 ;;
esac
""",
    )
    executable(
        python,
        """#!/bin/sh
printf '%s\n' "$PYTHONPATH" > "$FAKE_PYTHONPATH"
printf '%s\n' "$@" > "$FAKE_PYTHON_ARGS"
""",
    )
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_GIT_DIR": str(tmp_path / "linked-git-dir"),
        "FAKE_COMMON": str(common),
        "FAKE_PYTHONPATH": str(tmp_path / "pythonpath"),
        "FAKE_PYTHON_ARGS": str(tmp_path / "python.args"),
    }
    result = subprocess.run(
        [launch, "--active", "123"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "pythonpath").read_text().split(":")[0] == str(repo / "src")
    assert (tmp_path / "python.args").read_text().splitlines() == [
        "-m",
        "switchstand.launch",
        "--active",
        "123",
    ]
