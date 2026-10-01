"""Tests for the production edge replacement transaction."""
# pyright: reportPrivateUsage=false

import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

import switchstand.edge_maintenance as maintenance
from switchstand.edge_maintenance import (
    FASTMCP_STATE,
    Config,
    Failed,
    HostOperations,
    Interrupted,
    Unknown,
    _exclusive_lock,
    _validate_launch_mapping,
    deploy,
)
from switchstand.edge_monitor import HttpObservation


def config(
    tmp_path: Path,
    *,
    caddy: str = "http://127.0.0.1:1",
    public_origin: str = "https://public.invalid",
) -> Config:
    attempt = tmp_path / "attempt"
    attempt.mkdir(parents=True)
    return Config(
        attempt_dir=attempt,
        current_runtime=tmp_path / "current-runtime",
        current_sha="d" * 40,
        candidate_runtime=tmp_path / "runtime",
        candidate_sha="a" * 40,
        candidate_launcher=tmp_path / "candidate-launcher",
        candidate_launcher_sha="b" * 64,
        launcher=tmp_path / "launcher",
        current_launcher_sha="c" * 64,
        fastmcp_state=tmp_path / "fastmcp",
        env_file=tmp_path / "edge.env",
        caddy=caddy,
        public_origin=public_origin,
    )


class FakeOperations:
    def __init__(
        self,
        *,
        public_gate: bool = True,
        local: bool = True,
        public: bool = True,
        fail_at: str | None = None,
        unknown_at: str | None = None,
        rollback: bool = True,
    ):
        self.events: list[str] = []
        self.gated = False
        self.public_gate = public_gate
        self.local = local
        self.public = public
        self.fail_at = fail_at
        self.unknown_at = unknown_at
        self.rollback = rollback

    def _event(self, name: str) -> None:
        self.events.append(name)
        if self.fail_at == name:
            raise Failed("secret must never reach receipt")
        if self.unknown_at == name:
            raise Unknown("ambiguous")

    def preflight(self):
        self._event("preflight")

    def gate(self):
        self._event("gate")
        self.gated = True

    def gate_exact(self):
        self._event("gate_exact")
        return self.gated

    def public_gated(self):
        self._event("public_gated")
        return self.public_gate

    def stop(self):
        self._event("stop")

    def snapshot(self):
        self._event("snapshot")

    def swap(self):
        self._event("swap")

    def start(self):
        self._event("start")

    def local_ready(self):
        self._event("local_ready")
        return self.local

    def rollback_ready(self):
        self._event("rollback_ready")
        return self.rollback

    def ungate(self):
        self._event("ungate")
        self.gated = False

    def public_ready(self):
        self._event("public_ready")
        return self.public

    def restore_launcher(self):
        self._event("restore_launcher")


def receipt(subject: Config) -> dict[str, object]:
    return json.loads((subject.attempt_dir / "receipt.json").read_text())


def test_success_gates_every_public_path_before_stop(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations()

    assert deploy(subject, operations) == "PASS"

    assert operations.events == [
        "preflight",
        "gate",
        "public_gated",
        "stop",
        "snapshot",
        "swap",
        "start",
        "local_ready",
        "ungate",
        "public_ready",
    ]
    assert receipt(subject)["status"] == "PASS"


def test_unproved_public_gate_is_unknown_and_service_is_not_stopped(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(public_gate=False)

    assert deploy(subject, operations) == "UNKNOWN"

    assert "stop" not in operations.events
    assert operations.gated
    assert {key: receipt(subject)[key] for key in ("phase", "status", "error")} == {
        "phase": "GATED",
        "status": "UNKNOWN",
        "error": "Unknown",
    }


def test_candidate_failure_restores_old_launcher_before_ungating(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(local=False)

    assert deploy(subject, operations) == "FAIL"

    assert operations.events[-6:] == [
        "stop",
        "restore_launcher",
        "start",
        "rollback_ready",
        "ungate",
        "public_ready",
    ]
    assert not operations.gated
    assert receipt(subject)["phase"] == "ROLLED_BACK"


def test_rollback_ambiguity_keeps_maintenance_gate_and_reports_unknown(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(local=False, rollback=False)

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.gated
    assert receipt(subject)["error"] == "RollbackUnknown"


def test_interrupted_rollback_public_check_reinstalls_gate(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(local=False, unknown_at="public_ready")

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.events[-3:] == ["public_ready", "gate_exact", "gate"]
    assert operations.gated
    assert receipt(subject)["error"] == "RollbackUnknown"


def test_ambiguous_public_readback_reinstalls_gate(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(public=False)

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.events[-3:] == ["public_ready", "gate_exact", "gate"]
    assert operations.gated


def test_receipt_redacts_failure_detail(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(fail_at="preflight")

    assert deploy(subject, operations) == "FAIL"

    rendered = (subject.attempt_dir / "receipt.json").read_text()
    assert "secret" not in rendered
    assert receipt(subject)["error"] == "Failed"


def test_ambiguous_preflight_does_not_create_a_gate(tmp_path: Path):
    subject = config(tmp_path)
    operations = FakeOperations(unknown_at="preflight")

    assert deploy(subject, operations) == "UNKNOWN"

    assert operations.events == ["preflight"]
    assert not operations.gated


def test_rollback_restores_launcher_without_rewinding_oauth_state(tmp_path: Path):
    subject = config(tmp_path)
    subject.launcher.write_text("candidate")
    old = subject.attempt_dir / "launcher.before"
    old.write_text("old")
    oauth = subject.fastmcp_state
    oauth.mkdir()
    (oauth / "rotated-token").write_text("new-state")
    subject = replace(subject, current_launcher_sha=hashlib.sha256(b"old").hexdigest())

    HostOperations(subject).restore_launcher()

    assert subject.launcher.read_text() == "old"
    assert (oauth / "rotated-token").read_text() == "new-state"


def test_launch_mapping_rejects_wrong_runtime_or_oauth_store(tmp_path: Path):
    subject = config(tmp_path)
    subject.launcher.write_text(f"runtime = {subject.current_runtime}\n")
    subject.candidate_launcher.write_text(f"runtime = {subject.current_runtime}\n")
    with pytest.raises(Failed, match="runtime retarget"):
        _validate_launch_mapping(replace(subject, fastmcp_state=FASTMCP_STATE))

    subject.candidate_launcher.write_text(f"runtime = {subject.candidate_runtime}\n")
    with pytest.raises(Failed, match="production edge store"):
        _validate_launch_mapping(subject)

    _validate_launch_mapping(replace(subject, fastmcp_state=FASTMCP_STATE))


def test_running_process_binds_launcher_and_fastmcp_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pid = os.getpid()
    arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    launcher = Path(os.fsdecode(next(argument for argument in arguments if argument)))
    data_home = os.environ.get("XDG_DATA_HOME")
    expected_state = Path(
        os.environ.get("FASTMCP_HOME")
        or ((Path(data_home) if data_home else Path.home() / ".local/share") / "fastmcp")
    )
    subject = replace(config(tmp_path), launcher=launcher, fastmcp_state=expected_state)

    def main_pid(_command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout=f"{pid}\n", stderr="")

    monkeypatch.setattr(maintenance, "_run", main_pid)
    operations = HostOperations(subject)
    assert operations._running_process_exact()
    assert not HostOperations(
        replace(subject, launcher=tmp_path / "wrong")
    )._running_process_exact()
    assert not HostOperations(
        replace(subject, fastmcp_state=tmp_path / "wrong-state")
    )._running_process_exact()


def test_service_lock_contends_across_different_attempt_parents(tmp_path: Path):
    first = config(tmp_path / "first")
    second = config(tmp_path / "second")
    assert first.attempt_dir.parent != second.attempt_dir.parent
    lock = tmp_path / "service.lock"

    with (
        _exclusive_lock(lock),
        pytest.raises(SystemExit, match="another edge maintenance attempt"),
    ):
        _exclusive_lock(lock)


def test_gate_does_not_suppress_operator_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    operations = HostOperations(config(tmp_path))

    def interrupting_api(_method: str, _path: str, _body: object | None = None) -> object:
        raise Interrupted("stop")

    monkeypatch.setattr(operations, "_api", interrupting_api)
    with pytest.raises(Interrupted):
        operations.gate()


def test_ungate_does_not_suppress_operator_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    operations = HostOperations(config(tmp_path))

    def interrupting_api(_method: str, _path: str, _body: object | None = None) -> object:
        raise Interrupted("stop")

    monkeypatch.setattr(operations, "_api", interrupting_api)
    with pytest.raises(Interrupted):
        operations.ungate()


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_real_caddy_gate_covers_mcp_oauth_and_metadata_paths(tmp_path: Path):
    caddy = shutil.which("caddy")
    if caddy is None:
        pytest.skip("Caddy is not installed")
    admin_port, public_port = _unused_port(), _unused_port()
    caddy_config = tmp_path / "caddy.json"
    caddy_config.write_text(
        json.dumps(
            {
                "admin": {"listen": f"127.0.0.1:{admin_port}"},
                "apps": {
                    "http": {
                        "servers": {
                            "dish_action_router": {
                                "listen": [f"127.0.0.1:{public_port}"],
                                "routes": [
                                    {
                                        "handle": [
                                            {"handler": "static_response", "status_code": "418"}
                                        ]
                                    }
                                ],
                            }
                        }
                    }
                },
            }
        )
    )
    process = subprocess.Popen(
        [caddy, "run", "--config", str(caddy_config)],
        env={
            "HOME": str(tmp_path),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{admin_port}/config/", timeout=0.1)
                break
            except OSError:
                time.sleep(0.02)
        subject = config(
            tmp_path / "subject",
            caddy=f"http://127.0.0.1:{admin_port}",
            public_origin=f"http://127.0.0.1:{public_port}",
        )
        operations = HostOperations(subject)

        operations.gate()
        assert operations.gate_exact()
        assert operations.public_gated()
        for path in ("/switchstand/mcp", "/switchstand/mcp/maintenance-probe"):
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"http://127.0.0.1:{public_port}{path}", timeout=1)
            assert exc.value.code == 503
            assert exc.value.headers["Retry-After"] == "60"
        operations.ungate()
        assert not operations.gate_exact()
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_https_gate_proof_uses_public_dns_pinning(monkeypatch, tmp_path: Path):
    subject = config(tmp_path, public_origin="https://edge.example")
    operations = HostOperations(subject)
    requests = []

    monkeypatch.setattr(
        maintenance.ExternalIngressHttp,
        "public_addresses",
        lambda _self, host: ("8.8.8.8",) if host == "edge.example" else (),
    )

    def request(host, address, method, path):
        requests.append((host, address, method, path))
        return 503, {"retry-after": "60"}, b""

    monkeypatch.setattr(
        maintenance.ExternalIngressHttp, "request", staticmethod(request)
    )
    monkeypatch.setattr(
        maintenance.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("normal host DNS must not prove Funnel"),
    )

    assert operations.public_gated()
    assert len(requests) == len(maintenance.EDGE_PATHS)
    assert all(call[:3] == ("edge.example", "8.8.8.8", "GET") for call in requests)


def test_public_readiness_requires_external_ingress_probe(monkeypatch, tmp_path: Path):
    subject = config(tmp_path, public_origin="https://edge.example")
    operations = HostOperations(subject)
    monkeypatch.setattr(maintenance, "_sha", lambda _path: subject.candidate_launcher_sha)
    monkeypatch.setattr(operations, "_doctor", lambda *_args: True)

    monkeypatch.setattr(
        maintenance.ExternalIngressHttp,
        "observe",
        lambda _self: HttpObservation(False, None),
    )
    assert not operations.public_ready()

    monkeypatch.setattr(
        maintenance.ExternalIngressHttp,
        "observe",
        lambda _self: HttpObservation(True, 401, valid_auth_challenge=True),
    )
    assert operations.public_ready()
