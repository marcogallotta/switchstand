import os
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


def test_exact_ref_rejects_non_commit_object(monkeypatch: pytest.MonkeyPatch):
    source = parse_notes(NOTES)
    monkeypatch.setattr(launch_source, "_remote_sha", lambda _repo, _ref: CANDIDATE)
    monkeypatch.setattr(launch_source, "_local_ref", lambda _repo, _name: CANDIDATE)

    def fake_git(_repo: Path, *arguments: str, check: bool = True):
        if arguments[:2] == ("cat-file", "-e") and check:
            raise subprocess.CalledProcessError(1, ["git", *arguments])
        raise AssertionError(arguments)

    monkeypatch.setattr(launch_source, "_git", fake_git)
    with pytest.raises(LaunchSourceError, match="not an available commit"):
        launch_source._fetch_exact_ref(
            Path("."), "123", "candidate", source.candidate_ref, source.candidate_sha
        )


def test_prepare_source_rejects_task_base_before_fetch_when_control_differs(
    monkeypatch: pytest.MonkeyPatch,
):
    source = parse_notes(NOTES)

    def fake_git(_repo: Path, *arguments: str, check: bool = True):
        del check
        if arguments == ("rev-parse", "HEAD"):
            return _completed(stdout="c" * 40 + "\n")
        raise AssertionError(arguments)

    monkeypatch.setattr(launch_source, "_git", fake_git)
    monkeypatch.setattr(
        launch_source,
        "_fetch_exact_ref",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must fail before fetch")),
    )

    with pytest.raises(LaunchSourceError, match="task base does not match"):
        prepare_source(Path("."), "123", source, "c" * 40)


def test_prepare_source_rejects_wrong_control_checkout(monkeypatch: pytest.MonkeyPatch):
    source = parse_notes(NOTES)

    def fake_git(_repo: Path, *arguments: str, check: bool = True):
        del check
        if arguments == ("rev-parse", "HEAD"):
            return _completed(stdout="c" * 40 + "\n")
        raise AssertionError(arguments)

    monkeypatch.setattr(launch_source, "_git", fake_git)
    with pytest.raises(LaunchSourceError, match="not executing from the selected CONTROL SHA"):
        prepare_source(Path("."), "123", source, BASE)


def test_prepare_source_uses_selected_control_and_exact_remote_refs(
    monkeypatch: pytest.MonkeyPatch,
):
    source = parse_notes(NOTES)

    def fake_git(_repo: Path, *arguments: str, check: bool = True):
        del check
        if arguments == ("rev-parse", "HEAD"):
            return _completed(stdout=BASE + "\n")
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

    assert prepare_source(Path("."), "123", source, BASE) == source
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
