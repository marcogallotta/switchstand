import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from switchstand import launch_source
from switchstand.launch_source import LaunchSource, LaunchSourceError, parse_notes, prepare_source

BASE = "a" * 40
CANDIDATE = "b" * 40
NOTES = f"""SWITCHSTAND_REPOSITORY=marcogallotta/switchstand
SWITCHSTAND_BASE_REF=refs/heads/main
SWITCHSTAND_BASE_SHA={BASE}
SWITCHSTAND_CANDIDATE_REF=refs/pull/37/head
SWITCHSTAND_CANDIDATE_SHA={CANDIDATE}
"""


def _completed(*, stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], returncode, stdout, "")


def test_parse_notes_requires_complete_unique_exact_markers():
    source = parse_notes(NOTES)
    assert source == LaunchSource(
        "marcogallotta/switchstand",
        "refs/heads/main",
        BASE,
        "refs/pull/37/head",
        CANDIDATE,
    )

    with pytest.raises(LaunchSourceError, match="duplicate"):
        parse_notes(NOTES + f"SWITCHSTAND_BASE_SHA={BASE}\n")
    with pytest.raises(LaunchSourceError, match="missing exact source markers"):
        parse_notes(NOTES.replace(f"SWITCHSTAND_CANDIDATE_SHA={CANDIDATE}\n", ""))
    with pytest.raises(LaunchSourceError, match="base ref"):
        parse_notes(NOTES.replace("refs/heads/main", "refs/heads/other"))
    with pytest.raises(LaunchSourceError, match="candidate ref"):
        parse_notes(NOTES.replace("refs/pull/37/head", "refs/tags/release"))


def test_exact_ref_rejects_remote_movement_before_local_mutation(monkeypatch: pytest.MonkeyPatch):
    source = parse_notes(NOTES)
    monkeypatch.setattr(launch_source, "_remote_sha", lambda _repo, _ref: "c" * 40)

    def no_local_ref(_repo: Path, _name: str) -> str | None:
        raise AssertionError("local launch ref must not be inspected after remote movement")

    monkeypatch.setattr(launch_source, "_local_ref", no_local_ref)
    with pytest.raises(LaunchSourceError, match="launch candidate moved"):
        launch_source._fetch_exact_ref(
            Path("."), "123", "candidate", source.candidate_ref, source.candidate_sha
        )


def test_prepare_source_rejects_candidate_control_change(monkeypatch: pytest.MonkeyPatch):
    source = parse_notes(NOTES)

    def fake_git(_repo: Path, *arguments: str, check: bool = True):
        del check
        if arguments == ("remote", "get-url", "origin"):
            return _completed(stdout="git@github.com:marcogallotta/switchstand.git\n")
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            return _completed()
        raise AssertionError(arguments)

    monkeypatch.setattr(launch_source, "_git", fake_git)
    monkeypatch.setattr(launch_source, "_fetch_exact_ref", lambda *_args: None)
    monkeypatch.setattr(launch_source, "_control_changed", lambda *_args: True)

    with pytest.raises(LaunchSourceError, match="candidate changes launch/control"):
        prepare_source(Path("."), "123", source)


def test_prepare_source_accepts_stale_primary_when_control_is_identical(
    monkeypatch: pytest.MonkeyPatch,
):
    source = parse_notes(NOTES)

    def fake_git(_repo: Path, *arguments: str, check: bool = True):
        del check
        if arguments == ("remote", "get-url", "origin"):
            return _completed(stdout="https://github.com/marcogallotta/switchstand.git\n")
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            return _completed()
        raise AssertionError(arguments)

    fetched: list[tuple[str, str]] = []
    monkeypatch.setattr(launch_source, "_git", fake_git)
    monkeypatch.setattr(
        launch_source,
        "_fetch_exact_ref",
        lambda _repo, _task, label, _ref, sha: fetched.append((label, sha)),
    )
    monkeypatch.setattr(launch_source, "_control_changed", lambda *_args: False)

    assert prepare_source(Path("."), "123", source) == source
    assert fetched == [("base", BASE), ("candidate", CANDIDATE)]


def test_control_python_does_not_execute_candidate_startup_or_indirect_import(tmp_path: Path):
    control = tmp_path / "control"
    candidate = tmp_path / "candidate"
    control_package = control / "src" / "switchstand"
    candidate_package = candidate / "src" / "switchstand"
    control_package.mkdir(parents=True)
    candidate_package.mkdir(parents=True)
    control_marker = tmp_path / "control-marker"
    candidate_marker = tmp_path / "candidate-marker"

    (control_package / "__init__.py").write_text("")
    (control_package / "launch.py").write_text("import switchstand.run\n")
    (control_package / "run.py").write_text(
        "import os\nfrom pathlib import Path\nPath(os.environ['CONTROL_MARKER']).write_text('control')\n"
    )
    (candidate / "sitecustomize.py").write_text(
        "import os\nfrom pathlib import Path\nPath(os.environ['CANDIDATE_MARKER']).write_text('cwd')\n"
    )
    (candidate / "src" / "sitecustomize.py").write_text(
        "import os\nfrom pathlib import Path\nPath(os.environ['CANDIDATE_MARKER']).write_text('src')\n"
    )
    (candidate_package / "__init__.py").write_text("")
    (candidate_package / "run.py").write_text(
        "import os\nfrom pathlib import Path\nPath(os.environ['CANDIDATE_MARKER']).write_text('run')\n"
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(control / "src")
    env["CONTROL_MARKER"] = str(control_marker)
    env["CANDIDATE_MARKER"] = str(candidate_marker)
    subprocess.run(
        [sys.executable, "-P", "-m", "switchstand.launch"],
        cwd=candidate,
        env=env,
        check=True,
    )

    assert control_marker.read_text() == "control"
    assert not candidate_marker.exists()


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repo, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def test_worktree_exact_head_preserves_green_baseline_and_rejects_advanced_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    if shutil.which("git") is None:
        pytest.skip("git is required for exact launch worktree tests")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "switchstand@example.invalid")
    _git(repo, "config", "user.name", "Switchstand Test")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-c", "candidate")
    (repo / "candidate.txt").write_text("candidate\n")
    _git(repo, "add", "candidate.txt")
    _git(repo, "commit", "-m", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "main")

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    script = Path(__file__).parents[1] / "scripts" / "switchstand-worktree"
    first = subprocess.run(
        [str(script), "task-123", base, candidate],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    target = Path(first.stdout.strip())
    git_dir = Path(_git(target, "rev-parse", "--absolute-git-dir"))
    assert _git(target, "rev-parse", "HEAD") == candidate
    assert (git_dir / "switchstand-green-sha").read_text().strip() == base
    assert (git_dir / "switchstand-candidate-sha").read_text().strip() == candidate

    subprocess.run(
        [str(script), "task-123", base, candidate],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    (target / "later.txt").write_text("later\n")
    _git(target, "add", "later.txt")
    _git(target, "commit", "-m", "advance")
    rejected = subprocess.run(
        [str(script), "task-123", base, candidate],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 1
    assert "not a clean registered linked worktree" in rejected.stderr
