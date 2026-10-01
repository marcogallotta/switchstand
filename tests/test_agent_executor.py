import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from switchstand.agent_broker import Broker, Budget, Pressure
from switchstand.agent_executor import HOST, SANDBOX, Executor

GREEN = Pressure(16_000, 8_000, 4_000, 0, 0)


def reserve(tmp_path: Path, children: bool = False) -> Broker:
    broker = Broker(tmp_path)
    broker.initialize(Budget(4096, 6144, 512, 400, 512, 4, 1))
    child = 1 if children else 0
    body = (
        '{"request_id":"r1","parent":"root","worker":"leaf","worker_class":"light",'
        f'"children":{{"memory_high_mib":{700 * child},"memory_max_mib":{1024 * child},'
        f'"swap_max_mib":{128 * child},"cpu_percent":{100 * child},"tasks":{96 * child},'
        f'"workers":{child},"heavy":0}}}}'
    )
    request = tmp_path / "inboxes/root/r1.json"
    request.write_text(body)
    request.chmod(0o600)
    assert broker.ingest("root", "r1", GREEN)["state"] == "reserved"
    return broker


def test_executor_uses_exact_lease_limits_and_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = reserve(tmp_path)
    seen: list[str] = []

    def run(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        seen.extend(arguments)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    receipt = Executor(broker).run("leaf", ["python", "-V"], tmp_path)
    assert receipt["state"] == "completed"
    assert seen[:6] == [
        "systemd-run",
        "--user",
        f"--machine={HOST}",
        "--wait",
        "--pipe",
        "--collect",
    ]
    assert "--property=MemoryMax=1024M" in seen
    assert "--property=MemorySwapMax=128M" in seen
    assert "--property=CPUQuota=100%" in seen
    assert "--property=TasksMax=96" in seen
    assert "--property=RuntimeMaxSec=3600s" in seen
    assert f"--property=BindReadOnlyPaths={tmp_path}" in seen
    assert all(f"--property={value}" in seen for value in SANDBOX)
    assert seen[-8:] == [
        "--",
        "/usr/bin/env",
        "-i",
        "HOME=/tmp",
        "PATH=/usr/bin:/bin",
        "TMPDIR=/tmp",
        "python",
        "-V",
    ]
    assert broker.status()["leases"]["leaf"]["state"] == "completed"
    assert (tmp_path / "executions/leaf/status.json").is_file()


def test_non_leaf_is_not_launched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = reserve(tmp_path, children=True)
    called = False

    def run(*args: object, **kwargs: object) -> SimpleNamespace:
        nonlocal called
        called = True
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="leaf"):
        Executor(broker).run("leaf", ["true"], tmp_path)
    assert called is False
    assert broker.status()["leases"]["leaf"]["state"] == "reserved"


def test_confirmed_timeout_stops_unit_then_cancels_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = reserve(tmp_path)
    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(arguments)
        if arguments[0] == "systemd-run":
            raise subprocess.TimeoutExpired(arguments, 1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    receipt = Executor(broker).run("leaf", ["sleep", "60"], tmp_path, timeout=1)
    assert receipt["state"] == "timed_out"
    assert calls[1][:4] == ["systemctl", "--user", f"--machine={HOST}", "stop"]
    assert broker.status()["leases"]["leaf"]["state"] == "cancelled"


def test_unconfirmed_timeout_preserves_unknown_and_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = reserve(tmp_path)

    def run(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        if arguments[0] == "systemd-run":
            raise subprocess.TimeoutExpired(arguments, 1)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(subprocess, "run", run)
    receipt = Executor(broker).run("leaf", ["sleep", "60"], tmp_path, timeout=1)
    assert receipt["state"] == "unknown"
    assert broker.status()["leases"]["leaf"]["state"] == "reserved"


def test_launch_error_is_durable_and_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = reserve(tmp_path)
    calls = 0

    def run(*args: object, **kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", run)
    assert Executor(broker).run("leaf", ["true"], tmp_path)["state"] == "not_started"
    with pytest.raises(FileExistsError):
        Executor(broker).run("leaf", ["true"], tmp_path)
    assert calls == 1
