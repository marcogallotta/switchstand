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
FROZEN_REPOSITORY_FILES = (
    ".codex/config.toml",
    "scripts/codex-dispatch",
    "scripts/codex-coordinator-profile",
    "scripts/codex-hook",
    "scripts/coordinator-control",
    "scripts/coordinator-handoff",
)


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
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", remote], check=True, capture_output=True
    )
    primary.mkdir(parents=True)
    subprocess.run(["git", "-C", primary, "init", "-b", "main"], check=True, capture_output=True)
    for repo in (primary,):
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.invalid")
    for relative in FROZEN_REPOSITORY_FILES:
        path = primary / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative + "\n")
    (primary / "tracked.txt").write_text("start\n")
    git(primary, "add", ".")
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
    executable(home / ".codex/packages/standalone/current/bin/codex", "#!/bin/sh\nexit 0\n")
    control_home = home / ".local/state/switchstand/codex/coordinator"
    (control_home / "switchstand-coordinator-preferences.config.toml").write_text("profile\n")
    (control_home / "hooks.json").write_text("{}\n")
    executable(
        home / ".local/bin/codex",
        "#!/usr/bin/env python3\n"
        "import hashlib, json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "pathlib.Path(os.environ['HOME'], 'canary-calls').write_text('\\n'.join(args) + '\\n')\n"
        "digest = hashlib.sha256()\n"
        "for value in args:\n"
        "    encoded = value.encode()\n"
        "    digest.update(len(encoded).to_bytes(8, 'big'))\n"
        "    digest.update(encoded)\n"
        "proof = {'schema_version': 1, 'session': {"
        f"'generation': 'test-generation', 'start_commit': {output_commit!r}, "
        f"'repository': {str(home / 'switchstand')!r}, "
        "'invocation_digest': digest.hexdigest()}}\n"
        f"relative = {FROZEN_REPOSITORY_FILES!r}\n"
        "home = pathlib.Path(os.environ['HOME'])\n"
        "repo = home / 'switchstand'\n"
        "paths = {**{'repository:' + name: repo / name for name in relative}, "
        "'host:codex-shim': pathlib.Path(__file__).resolve(), "
        "'host:codex-executable': home / '.codex/packages/standalone/current/bin/codex', "
        "'generated:profile': home / '.local/state/switchstand/codex/coordinator/switchstand-coordinator-preferences.config.toml', "
        "'generated:hooks': home / '.local/state/switchstand/codex/coordinator/hooks.json'}\n"
        "proof['frozen_controls'] = [{'id': key, 'path': str(path), "
        "'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} "
        "for key, path in paths.items()]\n"
        "unsigned = json.dumps(proof, sort_keys=True, separators=(',', ':')).encode()\n"
        "proof['manifest_digest'] = hashlib.sha256(unsigned).hexdigest()\n"
        "pathlib.Path(os.environ['SWITCHSTAND_COORDINATOR_PROOF_PATH']).write_text(json.dumps(proof))\n"
        "final = {'status': 'SWITCHSTAND_COORDINATOR_CANARY_OK', "
        f"'start_commit': {output_commit!r}, 'session_generation': 'test-generation', "
        "'manifest_digest': proof['manifest_digest']}\n"
        "output = pathlib.Path(args[args.index('--output-last-message') + 1])\n"
        "output.write_text(json.dumps(final))\n"
        f"raise SystemExit({exit_code})\n",
    )
    return calls


def test_fast_forwards_clean_main_and_proves_fresh_agent(tmp_path: Path) -> None:
    home, primary, record, started, final = setup(tmp_path)
    calls = install_canary(home, final)

    result = subprocess.run(
        [SCRIPT, record],
        env=os.environ | {"HOME": str(home)},
        text=True,
        capture_output=True,
        check=False,
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
        "schema.json",
        "final.json",
        "trace.jsonl",
        "stderr.log",
        "result.json",
        "launch-manifest.json",
    }
    schema = json.loads((artifact / "schema.json").read_text())
    assert schema["properties"]["status"] == {
        "type": "string",
        "const": "SWITCHSTAND_COORDINATOR_CANARY_OK",
    }
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in artifact.iterdir())


def test_dirty_main_fails_before_fetch_or_canary(tmp_path: Path) -> None:
    home, primary, record, _started, final = setup(tmp_path)
    calls = install_canary(home, final)
    (primary / "untracked.txt").write_text("preserve me\n")

    result = subprocess.run(
        [SCRIPT, record],
        env=os.environ | {"HOME": str(home)},
        text=True,
        capture_output=True,
        check=False,
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
        [SCRIPT, record],
        env=os.environ | {"HOME": str(home)},
        text=True,
        capture_output=True,
        check=False,
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
