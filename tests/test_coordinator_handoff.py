from __future__ import annotations

import json
import os
import runpy
import stat
import subprocess
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/coordinator-handoff"


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", repo, *arguments], text=True).strip()


def executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def setup(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    home = tmp_path / "home"
    primary = home / "switchstand"
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", remote], check=True,
                   capture_output=True)
    primary.mkdir(parents=True)
    subprocess.run(["git", "-C", primary, "init", "-b", "main"], check=True,
                   capture_output=True)
    for repo in (primary,):
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.invalid")
    (primary / "tracked.txt").write_text("start\n")
    git(primary, "add", "tracked.txt")
    git(primary, "commit", "-m", "start")
    started = git(primary, "rev-parse", "HEAD")
    git(primary, "remote", "add", "origin", str(remote))
    git(primary, "push", "-u", "origin", "main")
    subprocess.run(["git", "clone", remote, source], check=True, capture_output=True)
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "tracked.txt").write_text("final\n")
    git(source, "add", "tracked.txt")
    git(source, "commit", "-m", "final")
    final = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "main")
    record = home / ".local/state/switchstand/codex/coordinator/start-commit.test"
    record.parent.mkdir(parents=True)
    record.write_text(started + "\n")
    record.chmod(0o600)
    return home, primary, record, started, final


def install_canary(home: Path, output_commit: str, exit_code: int = 0) -> Path:
    calls = home / "canary-calls"
    executable(
        home / ".local/bin/codex",
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$HOME/canary-calls\"\n"
        "output=\n"
        "while [ $# -gt 0 ]; do\n"
        "  if [ \"$1\" = --output-last-message ]; then output=$2; shift 2; else shift; fi\n"
        "done\n"
        f"printf '%s\\n' '{json.dumps({'status': 'SWITCHSTAND_COORDINATOR_CANARY_OK', 'start_commit': output_commit})}' > \"$output\"\n"
        f"exit {exit_code}\n",
    )
    return calls


def test_fast_forwards_clean_main_and_proves_fresh_agent(tmp_path: Path) -> None:
    home, primary, record, started, final = setup(tmp_path)
    calls = install_canary(home, final)

    result = subprocess.run(
        [SCRIPT, record], env=os.environ | {"HOME": str(home)},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert git(primary, "rev-parse", "HEAD") == final
    assert git(primary, "status", "--porcelain") == ""
    assert f"{started}..{final}" in result.stdout
    assert f"Fresh-agent canary: PASS at {final}" in result.stdout
    arguments = calls.read_text().splitlines()
    assert arguments[:3] == ["exec", "--ephemeral", "--json"]
    assert "--output-schema" in arguments
    artifact = Path(result.stdout.split("Evidence: ", 1)[1].strip())
    assert artifact.parent.stat().st_mode & 0o777 == 0o700
    assert {path.name for path in artifact.iterdir()} == {
        "schema.json", "final.json", "trace.jsonl", "stderr.log", "result.json",
    }
    schema = json.loads((artifact / "schema.json").read_text())
    assert schema["properties"]["status"] == {
        "type": "string", "const": "SWITCHSTAND_COORDINATOR_CANARY_OK",
    }
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in artifact.iterdir())


def test_dirty_main_fails_before_fetch_or_canary(tmp_path: Path) -> None:
    home, primary, record, _started, final = setup(tmp_path)
    calls = install_canary(home, final)
    (primary / "untracked.txt").write_text("preserve me\n")

    result = subprocess.run(
        [SCRIPT, record], env=os.environ | {"HOME": str(home)},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode != 0
    assert "refuses dirty main" in result.stderr
    assert (primary / "untracked.txt").read_text() == "preserve me\n"
    assert not calls.exists()
    artifact = Path(result.stderr.strip().rsplit("evidence: ", 1)[1])
    evidence = json.loads((artifact / "result.json").read_text())
    assert evidence["status"] == "FAILED"
    assert evidence["stage"] == "validate-inputs"
    assert evidence["known_head"] == git(primary, "rev-parse", "HEAD")


def test_failed_canary_preserves_updated_main_and_evidence(tmp_path: Path) -> None:
    home, primary, record, _started, final = setup(tmp_path)
    install_canary(home, final, exit_code=7)

    result = subprocess.run(
        [SCRIPT, record], env=os.environ | {"HOME": str(home)},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode != 0
    assert git(primary, "rev-parse", "HEAD") == final
    assert "fresh-agent canary failed; evidence:" in result.stderr
    artifact = Path(result.stderr.strip().rsplit("evidence: ", 1)[1])
    assert (artifact / "stderr.log").exists()


def test_git_is_noninteractive_and_timeout_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(SCRIPT), run_name="coordinator_handoff_test")
    git_function = namespace["git"]
    seen: dict[str, Any] = {}

    def timeout_run(*args: Any, **kwargs: Any) -> NoReturn:
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(git_function.__globals__["subprocess"], "run", timeout_run)
    try:
        git_function(Path("/repo"), "fetch", "origin")
    except SystemExit as error:
        assert "git fetch origin timed out after 30s" in str(error)
    else:
        raise AssertionError("timeout must fail the handoff")
    assert seen["timeout"] == 30
    assert cast(dict[str, str], seen["env"])["GIT_TERMINAL_PROMPT"] == "0"
