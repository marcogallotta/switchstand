from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from switchstand import stage12_operator_cli as cli
from switchstand.edge_maintenance import FASTMCP_STATE, LOCK, Config, Unknown

SHA = "a" * 40


def args(tmp_path: Path, target: str) -> SimpleNamespace:
    return SimpleNamespace(
        target=target, attempt_dir=tmp_path / "attempt", current_runtime=tmp_path / "old",
        current_sha=SHA, candidate_runtime=tmp_path / "new", candidate_sha=SHA,
        candidate_launcher=tmp_path / "candidate-launcher", candidate_launcher_sha="b" * 64,
        launcher=tmp_path / "launcher", current_launcher_sha="c" * 64,
        fastmcp_state=FASTMCP_STATE, env_file=tmp_path / "edge.env",
    )


def test_production_config_is_explicit_and_exact(tmp_path: Path, monkeypatch):
    values = args(tmp_path, "production")
    observed: list[Config] = []
    monkeypatch.setattr(cli, "_validate_target", observed.append)

    config = cli._config(values)

    assert config.target == "production" and config.lock_path == LOCK
    assert observed == [config]


def test_disposable_config_comes_only_from_ready_descriptor(tmp_path: Path, monkeypatch):
    root = tmp_path / "proof"
    descriptor = {
        "fastmcp_state": str(root / "copied-state/fastmcp"),
        "edge_service": "switchstand-rehearsal-proof.service",
        "endpoints": {"caddy": "http://127.0.0.1:1", "local": "http://127.0.0.1:2/mcp",
                      "public": "http://127.0.0.1:3"},
    }
    values = args(tmp_path, "disposable:proof")
    values.fastmcp_state = None
    values.attempt_dir, values.launcher = root / "attempt", root / "launcher"
    values.candidate_launcher, values.env_file = root / "candidate", root / "edge.env"
    called: list[tuple[str, str, Path]] = []
    monkeypatch.setattr(cli, "load_ready", lambda name, sha, runtime: (
        called.append((name, sha, runtime)) or (root, descriptor)
    ))
    monkeypatch.setattr(cli, "_validate_target", lambda _config: None)

    config = cli._config(values)

    assert called == [("proof", SHA, values.candidate_runtime)]
    assert config.target_root == root and config.fastmcp_state == root / "copied-state/fastmcp"
    assert config.service == descriptor["edge_service"] and config.caddy == "http://127.0.0.1:1"
    values.fastmcp_state = tmp_path / "foreign"
    with pytest.raises(SystemExit, match="READY descriptor"):
        cli._config(values)


@pytest.mark.parametrize(("action", "existing", "present"), [
    ("prepare", False, False), ("prepare", True, True),
])
def test_prepare_attempt_is_explicit(tmp_path: Path, action: str, existing: bool, present: bool):
    config = args(tmp_path, "production")
    if present:
        config.attempt_dir.mkdir(mode=0o700)
    cli._attempt(SimpleNamespace(action=action, existing_attempt=existing), config)
    assert config.attempt_dir.is_dir()


def test_existing_attempt_flag_cannot_change_other_actions(tmp_path: Path):
    with pytest.raises(SystemExit, match="only with prepare"):
        cli._attempt(SimpleNamespace(action="resume", existing_attempt=True), args(tmp_path, "production"))


@pytest.mark.parametrize(("result", "expected"), [
    ("PASS", 0), ("FAIL", 1), ("UNKNOWN", 2), ("POSTGRES_AUTHORITY_UNKNOWN", 2),
])
def test_exit_preserves_unknown(result: str, expected: int):
    assert cli._exit(result) == expected


def test_observation_failure_is_unknown_inside_shared_lock(monkeypatch, capsys):
    values = SimpleNamespace(
        action="status", approved_worksheet_digest=None, existing_attempt=False,
        target="disposable:proof",
    )
    monkeypatch.setattr(cli, "_parser", lambda: SimpleNamespace(parse_args=lambda _argv: values))
    monkeypatch.setattr(cli, "_target_lock", lambda _target: Path("/lock"))
    events: list[str] = []

    @contextmanager
    def locked(_path):
        events.append("locked")
        try:
            yield
        finally:
            events.append("released")

    monkeypatch.setattr(cli, "_exclusive_lock", locked)
    monkeypatch.setattr(cli, "_run", lambda _args: (_ for _ in ()).throw(Unknown("lost")))
    assert cli.main([]) == 2
    assert capsys.readouterr().out == "UNKNOWN\n"
    assert events == ["locked", "released"]
