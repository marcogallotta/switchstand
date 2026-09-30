"""Tests for the inert combined-to-split activation transaction."""
# pyright: reportPrivateUsage=false

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

import switchstand.stable_auth_deployment as deployment
from switchstand.edge_maintenance import Failed, Unknown
from switchstand.stable_auth_deployment import (
    EDGE_PATHS,
    GATE_ID,
    ActivationConfig,
    CaddyRoutes,
    HostActivationOperations,
    _contains_id,
    _env,
    activate,
)


def config(tmp_path: Path) -> ActivationConfig:
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    return ActivationConfig(
        attempt_dir=attempt,
        current_runtime=tmp_path / "current",
        current_sha="c" * 40,
        candidate_runtime=tmp_path / "candidate",
        candidate_sha="d" * 40,
        runtime_python=tmp_path / "python",
        assets_dir=tmp_path / "assets",
        auth_environment_file=tmp_path / "auth.env",
        edge_environment_file=tmp_path / "edge.env",
        doctor_environment_file=tmp_path / "doctor.env",
        internal_secret_file=tmp_path / "internal.secret",
        legacy_token_file=tmp_path / "legacy.token",
        current_state=tmp_path / "current-state",
        migrated_state=tmp_path / "migrated-state",
        backup_state=tmp_path / "backup-state",
        unit_dir=tmp_path / "units",
    )


class FakeOperations:
    def __init__(
        self,
        *,
        fail_at: str | None = None,
        unknown_at: str | None = None,
        public_gate: bool = True,
        auth_ready: bool = True,
        edge_ready: bool = True,
        public_ready: bool = True,
        rollback: bool = True,
    ):
        self.events: list[str] = []
        self.gated = False
        self.fail_at = fail_at
        self.unknown_at = unknown_at
        self.public_gate = public_gate
        self.auth = auth_ready
        self.edge = edge_ready
        self.public = public_ready
        self.rollback = rollback

    def event(self, name: str) -> None:
        self.events.append(name)
        if name == self.fail_at:
            raise Failed("sensitive failure detail")
        if name == self.unknown_at:
            raise Unknown("ambiguous")

    def preflight(self):
        self.event("preflight")

    def gate(self):
        self.event("gate")
        self.gated = True

    def gate_exact(self):
        self.event("gate_exact")
        return self.gated

    def public_gated(self):
        self.event("public_gated")
        return self.public_gate

    def stop_combined(self):
        self.event("stop_combined")

    def prove_no_unknown_effects(self):
        self.event("prove_no_unknown_effects")

    def copy_state(self):
        self.event("copy_state")

    def install_split(self):
        self.event("install_split")

    def start_auth(self):
        self.event("start_auth")

    def auth_ready(self):
        self.event("auth_ready")
        return self.auth

    def start_edge(self):
        self.event("start_edge")

    def edge_ready(self):
        self.event("edge_ready")
        return self.edge

    def route_split(self):
        self.event("route_split")

    def ungate(self):
        self.event("ungate")
        self.gated = False

    def public_ready(self):
        self.event("public_ready")
        return self.public

    def rollback_pre_exposure(self):
        self.event("rollback_pre_exposure")
        if self.rollback:
            self.gated = False
        return self.rollback


def receipt(subject: ActivationConfig) -> dict[str, object]:
    return json.loads((subject.attempt_dir / "activation-receipt.json").read_text())


def test_success_quiesces_and_proves_recovery_before_starting_split(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations()

    assert activate(subject, operations) == "PASS"

    assert operations.events == [
        "preflight",
        "gate",
        "public_gated",
        "stop_combined",
        "prove_no_unknown_effects",
        "copy_state",
        "install_split",
        "start_auth",
        "auth_ready",
        "start_edge",
        "edge_ready",
        "route_split",
        "ungate",
        "public_ready",
    ]
    assert receipt(subject)["status"] == "PASS"
    assert receipt(subject)["automatic_oauth_restore"] is False


def test_unproved_gate_never_stops_the_combined_service(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(public_gate=False)

    assert activate(subject, operations) == "UNKNOWN"

    assert "stop_combined" not in operations.events
    assert operations.gated
    assert receipt(subject)["phase"] == "GATED"


@pytest.mark.parametrize(
    "failure",
    [
        "prove_no_unknown_effects",
        "copy_state",
        "install_split",
        "start_auth",
        "auth_ready",
        "start_edge",
        "edge_ready",
        "route_split",
    ],
)
def test_definite_pre_exposure_failure_restores_combined_service(
    tmp_path: Path,
    failure: str,
):
    subject = config(tmp_path)
    operations = FakeOperations(
        fail_at=failure if failure not in {"auth_ready", "edge_ready"} else None,
        auth_ready=failure != "auth_ready",
        edge_ready=failure != "edge_ready",
    )

    assert activate(subject, operations) == "FAIL"

    assert operations.events[-1] == "rollback_pre_exposure"
    assert not operations.gated
    assert receipt(subject)["phase"] == "ROLLED_BACK"


def test_ambiguous_mutation_keeps_gate_and_does_not_attempt_rollback(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(unknown_at="route_split")

    assert activate(subject, operations) == "UNKNOWN"

    assert operations.gated
    assert "rollback_pre_exposure" not in operations.events
    assert receipt(subject)["phase"] == "EDGE_STARTED"


def test_failed_public_readback_regates_and_preserves_new_oauth_state(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(public_ready=False)

    assert activate(subject, operations) == "UNKNOWN"

    assert operations.events[-2:] == ["gate_exact", "gate"]
    assert operations.gated
    assert "rollback_pre_exposure" not in operations.events
    assert receipt(subject)["phase"] == "UNGATED"
    assert receipt(subject)["automatic_oauth_restore"] is False


def test_rollback_ambiguity_stays_gated_and_redacts_exception_text(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(fail_at="copy_state", rollback=False)

    assert activate(subject, operations) == "UNKNOWN"

    assert operations.gated
    encoded = (subject.attempt_dir / "activation-receipt.json").read_text()
    assert "sensitive" not in encoded
    assert receipt(subject)["error"] == "RollbackUnknown"


def test_environment_parser_requires_owned_mode_0600_file(tmp_path: Path):
    environment = tmp_path / "service.env"
    environment.write_text("A=one\nB='two'\n")
    environment.chmod(0o600)
    assert _env(environment) == {"A": "one", "B": "two"}

    environment.chmod(0o644)
    with pytest.raises(Failed, match="mode-0600"):
        _env(environment)


def test_nested_caddy_identifier_detection_is_exact():
    route = {"handle": [{"handler": "subroute", "routes": [{"@id": "wanted"}]}]}
    assert _contains_id(route, {"wanted"})
    assert not _contains_id(route, {"want"})


@pytest.mark.parametrize("result", ("desired", "expected", "other"))
def test_caddy_mutation_reconciles_failed_request_by_exact_full_route_readback(
    result: str, monkeypatch: pytest.MonkeyPatch
):
    routes = CaddyRoutes(
        endpoint="http://127.0.0.1:1",
        public_origin="http://127.0.0.1:2",
        gate_id="gate",
        paths=("/mcp",),
        retry_after=17,
    )
    expected: list[object] = [{"@id": "old"}]
    desired: list[object] = [{"@id": "new"}]

    def api(method: str, _path: str, body: object | None = None):
        if method == "PATCH":
            raise Unknown("lost response")
        assert body is None
        return {"desired": desired, "expected": expected, "other": []}[result]

    monkeypatch.setattr(routes, "api", api)
    if result == "desired":
        routes._replace(expected, desired, "test")
    elif result == "expected":
        with pytest.raises(Failed, match="did not take effect"):
            routes._replace(expected, desired, "test")
    else:
        with pytest.raises(Unknown, match="ambiguous"):
            routes._replace(expected, desired, "test")


def test_running_split_identity_binds_python_module_to_candidate_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    subject = config(tmp_path)
    operations = HostActivationOperations(subject)
    monkeypatch.setattr(deployment, "_unit_state", lambda _service: ("active", 42, 0))
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _path: b"\0".join(
            (
                str(subject.runtime_python).encode(),
                b"-m",
                b"switchstand.stable_auth_runtime",
                b"auth",
                b"",
            )
        ),
    )
    monkeypatch.setattr(
        operations,
        "_process_environment",
        lambda _service: {"PYTHONPATH": str(subject.candidate_runtime / "src")},
    )

    assert operations._running_split_exact("auth.service", "auth")
    monkeypatch.setattr(
        operations,
        "_process_environment",
        lambda _service: {"PYTHONPATH": str(subject.current_runtime / "src")},
    )
    assert not operations._running_split_exact("auth.service", "auth")


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_real_caddy_route_replacement_remains_behind_exact_gate(tmp_path: Path):
    caddy = shutil.which("caddy")
    if caddy is None:
        pytest.skip("Caddy is not installed")
    admin_port, public_port = _unused_port(), _unused_port()
    original = [
        {
            "@id": "original",
            "match": [{"path": ["/switchstand/mcp"]}],
            "handle": [{"handler": "static_response", "status_code": "418"}],
            "terminal": True,
        }
    ]
    replacement = [
        {
            "@id": "replacement",
            "match": [{"path": ["/switchstand/mcp"]}],
            "handle": [{"handler": "static_response", "status_code": "204"}],
            "terminal": True,
        }
    ]
    config_file = tmp_path / "caddy.json"
    config_file.write_text(
        json.dumps(
            {
                "admin": {"listen": f"127.0.0.1:{admin_port}"},
                "apps": {
                    "http": {
                        "servers": {
                            "dish_action_router": {
                                "listen": [f"127.0.0.1:{public_port}"],
                                "routes": original,
                            }
                        }
                    }
                },
            }
        )
    )
    process = subprocess.Popen(
        [caddy, "run", "--config", str(config_file)],
        env=os.environ
        | {
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    routes = CaddyRoutes(
        endpoint=f"http://127.0.0.1:{admin_port}",
        public_origin=f"http://127.0.0.1:{public_port}",
        gate_id=GATE_ID,
        paths=EDGE_PATHS,
        retry_after=17,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if routes.routes() == original:
                    break
            except Unknown:
                time.sleep(0.05)
        else:
            pytest.fail("Caddy did not start")
        routes.gate(original)
        assert routes.gate_exact()
        assert routes.public_gated()
        routes.replace_under_gate(original, replacement)
        assert routes.routes() == [routes.gate_route, *replacement]
        routes.ungate(replacement)
        assert routes.routes() == replacement
    finally:
        process.terminate()
        process.wait(timeout=5)
