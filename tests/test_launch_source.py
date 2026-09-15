import os
import subprocess
import sys
from pathlib import Path

import pytest

from switchstand import launch_source
from switchstand.launch_source import (
    LaunchSource,
    LaunchSourceError,
    load_asana_token,
    parse_notes,
    prepare_source,
)

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


def test_load_asana_token_reads_only_exact_config_value(tmp_path: Path):
    config = tmp_path / "config"
    config.write_text("# host config\nDATABASE_URL=not-for-resolver\nASANA_TOKEN=host-only\n")
    assert load_asana_token(config) == "host-only"


@pytest.mark.parametrize("contents", ["", "ASANA_TOKEN=\n", "ASANA_TOKEN=one\nASANA_TOKEN=two\n"])
def test_load_asana_token_rejects_missing_empty_or_duplicate_value(
    tmp_path: Path, contents: str
):
    config = tmp_path / "config"
    config.write_text(contents)
    with pytest.raises(LaunchSourceError, match="ASANA_TOKEN is missing or empty"):
        load_asana_token(config)


@pytest.mark.parametrize("value", ["'quoted'", '"quoted"', "token # comment"])
def test_load_asana_token_rejects_non_plain_value_without_repeating_it(
    tmp_path: Path, value: str
):
    config = tmp_path / "config"
    config.write_text(f"ASANA_TOKEN={value}\n")
    with pytest.raises(LaunchSourceError, match="plain unquoted") as error:
        load_asana_token(config)
    assert value not in str(error.value)


def test_launch_source_main_uses_protected_config_instead_of_ambient_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    config = tmp_path / ".config" / "switchstand" / ".env"
    config.parent.mkdir(parents=True)
    config.write_text("ASANA_TOKEN=host-only\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASANA_TOKEN", "ambient-should-be-ignored")
    monkeypatch.setattr(sys, "argv", ["launch_source", "--repo", str(tmp_path),
                                      "--control-sha", BASE, "123"])
    observed: list[str] = []

    def fake_resolve(repo: Path, active: str, token: str, control_sha: str) -> LaunchSource:
        assert repo == tmp_path
        assert active == "123"
        assert control_sha == BASE
        observed.append(token)
        return parse_notes(NOTES)

    monkeypatch.setattr(launch_source, "resolve", fake_resolve)
    launch_source.main()
    assert observed == ["host-only"]
    assert capsys.readouterr().out == f"{BASE} {CANDIDATE}\n"


def test_launch_source_main_reports_missing_protected_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["launch_source", "--repo", str(tmp_path),
                                      "--control-sha", BASE, "123"])
    with pytest.raises(SystemExit) as error:
        launch_source.main()
    assert error.value.code == 1
    assert "protected Asana config is unavailable" in capsys.readouterr().err


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


def test_local_ref_uses_real_git_missing_ref_exit_code(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    name = "refs/switchstand/launch/123/base"

    assert launch_source._local_ref(tmp_path, name) is None

    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "fixture"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(tmp_path), "update-ref", name, sha], check=True)

    assert launch_source._local_ref(tmp_path, name) == sha


def test_exact_ref_fetches_on_first_use_with_real_git(tmp_path: Path):
    remote = tmp_path / "remote"
    control = tmp_path / "control"
    subprocess.run(["git", "init", "-q", "-b", "main", str(remote)], check=True)
    subprocess.run(
        ["git", "-C", str(remote), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "fixture"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "init", "-q", str(control)], check=True)
    subprocess.run(["git", "-C", str(control), "remote", "add", "origin", str(remote)], check=True)
    name = "refs/switchstand/launch/123/base"

    assert launch_source._local_ref(control, name) is None
    launch_source._fetch_exact_ref(control, "123", "base", "refs/heads/main", sha)
    assert launch_source._local_ref(control, name) == sha


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
