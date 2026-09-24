import os
import subprocess
import sys
from pathlib import Path

import pytest

from switchstand import launch_source
from switchstand.launch_source import LaunchSource, LaunchSourceError, load_asana_token, parse_notes

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
