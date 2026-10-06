import inspect
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from switchstand.agent_broker import Broker, Budget, Pressure
from switchstand.agent_executor import ManagedExecutor
from switchstand.managed_launch import (
    MANAGED_ROOT_BUDGET,
    ManagedParentLauncher,
    PreparedLaunchStore,
    managed_parent_command,
)
from switchstand.managed_reentry import MANAGED_DEVELOPER_INSTRUCTIONS

WORK_ID = UUID("11111111-1111-4111-8111-111111111111")
GRANT_ID = UUID("22222222-2222-4222-8222-222222222222")
GREEN = Pressure(16_000, 12_000, 4_000, 3_900, 0, swap_activity_pages=0)


def prove_empty_cgroup(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *args, **kwargs: (
            "populated 0\n" if path.name == "cgroup.events" else original(path, *args, **kwargs)
        ),
    )


def prepared(tmp_path: Path) -> tuple[Broker, Path]:
    root = tmp_path / "broker"
    broker = Broker(root)
    broker.initialize(Budget(4096, 6144, 512, 400, 512, 4, 1))
    request = root / "inboxes/root/request.json"
    request.write_text(
        json.dumps(
            {
                "request_id": "request",
                "parent": "root",
                "worker": "managed-parent",
                "worker_class": "light",
                "children": {
                    "memory_high_mib": 700,
                    "memory_max_mib": 1024,
                    "swap_max_mib": 128,
                    "cpu_percent": 100,
                    "tasks": 96,
                    "workers": 1,
                    "heavy": 0,
                },
            }
        )
    )
    request.chmod(0o600)
    reservation = broker.ingest("root", "request", GREEN)
    control = tmp_path / "control"
    writer = tmp_path / "writer"
    codex_home = tmp_path / "codex-home"
    control.mkdir(mode=0o755)
    writer.mkdir(mode=0o700)
    codex_home.mkdir(mode=0o700)
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    manifest = PreparedLaunchStore(broker).prepare(
        work_id=WORK_ID,
        grant_id=GRANT_ID,
        grant_version=7,
        lease_id="managed-parent",
        reservation_id=reservation["reservation_id"],
        control=control,
        writer=writer,
        codex_home=codex_home,
        codex_executable=executable,
        assignment="implement the exact task",
        priority_claims=True,
    )
    return broker, manifest


def persist_execution_receipt(
    broker: Broker, path: Path, state: str
) -> tuple[dict[str, object], float]:
    manifest = PreparedLaunchStore(broker).load(path)
    receipt: dict[str, object] = {
        "launch_id": manifest.launch_id,
        "lease_id": manifest.lease_id,
        "reservation_id": manifest.reservation_id,
        "attempt_id": manifest.attempt_id,
        "unit": manifest.unit,
        "work_id": str(manifest.work_id),
        "grant_id": str(manifest.grant_id),
        "grant_version": manifest.grant_version,
        "state": "starting",
    }
    broker.attach_execution(
        manifest.lease_id,
        reservation_id=manifest.reservation_id,
        attempt_id=manifest.attempt_id,
        unit=manifest.unit,
        receipt=receipt,
        claim_ttl_seconds=5,
    )
    receipt["state"] = state
    broker.record_execution(manifest.lease_id, receipt)
    return receipt, float(broker.lease(manifest.lease_id)["claim_expires_monotonic"])


def test_prepared_manifest_is_exact_sealed_and_has_only_canonical_command(tmp_path: Path) -> None:
    broker, path = prepared(tmp_path)
    manifest = PreparedLaunchStore(broker).load(path)

    assert manifest.work_id == WORK_ID
    assert manifest.grant_id == GRANT_ID
    assert manifest.grant_version == 7
    assert manifest.priority_claims is True
    assert manifest.reservation_id == broker.lease("managed-parent")["reservation_id"]
    assert manifest.command[:4] == (str(tmp_path / "codex"), "exec", "-C", str(tmp_path / "writer"))
    assert "--dangerously-bypass-approvals-and-sandbox" not in manifest.command
    instructions = next(
        value for value in manifest.command if value.startswith("developer_instructions=")
    )
    assert instructions == "developer_instructions=" + json.dumps(
        MANAGED_DEVELOPER_INSTRUCTIONS
    )
    assert "review uses its current review procedure" in instructions
    assert not any(value.startswith("hooks.SessionStart=") for value in manifest.command)
    enabled = next(
        value for value in manifest.command
        if value.startswith("mcp_servers.switchstand.enabled_tools=")
    )
    assert "priority_claim_get" in enabled
    assert "priority_claim_record" in enabled
    assert "priority_context_get" in enabled
    forwarded = next(
        value for value in manifest.command
        if value.startswith("mcp_servers.switchstand.env_vars=")
    )
    assert "SWITCHSTAND_PRIORITY_CLAIMS" in forwarded
    assert "--json" in manifest.command
    assert "command" not in inspect.signature(PreparedLaunchStore.prepare).parameters


def test_managed_command_omits_priority_tools_without_explicit_opt_in(tmp_path: Path) -> None:
    command = managed_parent_command(
        tmp_path / "codex", tmp_path / "control", tmp_path / "writer",
        "implement the exact task",
    )
    enabled = next(
        value for value in command
        if value.startswith("mcp_servers.switchstand.enabled_tools=")
    )
    assert "priority_claim_get" not in enabled
    assert not any(
        "SWITCHSTAND_PRIORITY_CLAIMS" in value for value in command
    )


def test_manifest_tampering_and_wrong_store_are_rejected(tmp_path: Path) -> None:
    broker, path = prepared(tmp_path)
    envelope = json.loads(path.read_text())
    envelope["payload"]["grant_version"] = 8
    path.write_text(json.dumps(envelope))
    path.chmod(0o600)

    with pytest.raises(ValueError, match="seal mismatch"):
        PreparedLaunchStore(broker).load(path)
    with pytest.raises(ValueError, match="outside"):
        PreparedLaunchStore(broker).load(tmp_path / "other.json")


def test_private_launch_paths_and_exact_reservation_are_required(tmp_path: Path) -> None:
    broker, _ = prepared(tmp_path)
    store = PreparedLaunchStore(broker)
    writer = tmp_path / "unsafe-writer"
    writer.mkdir(mode=0o755)

    values = {
        "work_id": WORK_ID,
        "grant_id": GRANT_ID,
        "grant_version": 7,
        "lease_id": "managed-parent",
        "control": tmp_path / "control",
        "codex_home": tmp_path / "codex-home",
        "codex_executable": tmp_path / "codex",
        "assignment": "task",
    }
    with pytest.raises(ValueError, match="reservation identity"):
        store.prepare(
            reservation_id="wrong",
            writer=tmp_path / "writer",
            **values,
        )
    with pytest.raises(PermissionError, match="unsafe"):
        store.prepare(
            reservation_id=broker.lease("managed-parent")["reservation_id"],
            writer=writer,
            **values,
        )


def test_managed_executor_uses_aggregate_budget_and_exact_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prove_empty_cgroup(monkeypatch)
    broker, path = prepared(tmp_path)
    calls: list[list[str]] = []

    def run(arguments: list[str], **_: object) -> SimpleNamespace:
        calls.append(arguments)
        if "--property=ActiveState" in arguments:
            return SimpleNamespace(returncode=0, stdout="inactive\n")
        if "--property=ControlGroup" in arguments:
            return SimpleNamespace(returncode=0, stdout="/missing-test-cgroup\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    receipt = ManagedExecutor(broker).run(path, timeout=60)

    assert receipt["state"] == "completed"
    assert broker.status()["leases"]["managed-parent"]["state"] == "completed"
    start = calls[0]
    assert "--property=MemoryMax=2048M" in start
    assert f"--property=BindReadOnlyPaths={tmp_path / 'control'}" in start
    assert f"--property=BindPaths={tmp_path / 'writer'}" in start
    assert f"--property=BindPaths={tmp_path / 'codex-home'}" in start
    assert f"ACTIVE_WORK_ID={WORK_ID}" in start
    assert f"SWITCHSTAND_GRANT_ID={GRANT_ID}" in start
    assert "SWITCHSTAND_PRIORITY_CLAIMS=1" in start
    separator = start.index("--")
    assert start[separator + 1 : separator + 4] == [
        "/usr/bin/env",
        "-i",
        f"HOME={tmp_path / 'codex-home'}",
    ]
    assert str(tmp_path / "codex") in start[separator:]


def test_managed_timeout_releases_only_after_terminal_empty_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prove_empty_cgroup(monkeypatch)
    broker, path = prepared(tmp_path)

    def run(arguments: list[str], **_: object) -> SimpleNamespace:
        if arguments[0] == "systemd-run":
            raise subprocess.TimeoutExpired(arguments, 60)
        if "--property=ActiveState" in arguments:
            return SimpleNamespace(returncode=0, stdout="inactive\n")
        if "--property=ControlGroup" in arguments:
            return SimpleNamespace(returncode=0, stdout="/missing-test-cgroup\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    receipt = ManagedExecutor(broker).run(path, timeout=60)

    assert receipt["state"] == "timed_out"
    assert broker.status()["leases"]["managed-parent"]["state"] == "cancelled"


def test_managed_signal_stops_and_reconciles_before_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prove_empty_cgroup(monkeypatch)
    broker, path = prepared(tmp_path)

    def run(arguments: list[str], **_: object) -> SimpleNamespace:
        if arguments[0] == "systemd-run":
            raise KeyboardInterrupt
        if "--property=ActiveState" in arguments:
            return SimpleNamespace(returncode=0, stdout="inactive\n")
        if "--property=ControlGroup" in arguments:
            return SimpleNamespace(returncode=0, stdout="/test-cgroup\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    receipt = ManagedExecutor(broker).run(path, timeout=60)
    assert receipt["state"] == "timed_out"
    assert broker.status()["leases"]["managed-parent"]["state"] == "cancelled"


def test_managed_executor_preserves_reservation_when_runtime_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker, path = prepared(tmp_path)

    def run(arguments: list[str], **_: object) -> SimpleNamespace:
        if "show" in arguments:
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(subprocess, "run", run)
    receipt = ManagedExecutor(broker).run(path, timeout=60)

    assert receipt["state"] == "unknown"
    assert broker.lease("managed-parent")["state"] == "execution_active"


def test_persisted_not_started_executor_can_be_recovered_after_claim_expiry(
    tmp_path: Path,
) -> None:
    broker, path = prepared(tmp_path)
    _, expires = persist_execution_receipt(broker, path, "not_started")

    recovered = ManagedExecutor(broker).recover_execution(
        path, observed_monotonic=expires
    )

    assert recovered == {"state": "released", "reason": "abandoned"}
    assert broker.status()["leases"]["managed-parent"]["state"] == "abandoned"


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        ({"attempt_id": "different-attempt"}, "receipt_identity"),
        ({"state": "unknown"}, "receipt_ambiguous"),
    ],
)
def test_not_started_recovery_holds_mismatched_or_unknown_receipt(
    tmp_path: Path, changed: dict[str, object], reason: str
) -> None:
    broker, path = prepared(tmp_path)
    receipt, expires = persist_execution_receipt(broker, path, "not_started")
    broker.record_execution("managed-parent", {**receipt, **changed})

    recovered = ManagedExecutor(broker).recover_execution(
        path, observed_monotonic=expires
    )

    assert recovered == {"state": "unknown", "reason": reason}
    assert broker.lease("managed-parent")["state"] == "execution_active"


def test_lost_executor_recovery_uses_exact_terminal_empty_runtime_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker, path = prepared(tmp_path)
    _, expires = persist_execution_receipt(broker, path, "starting")
    manifest = PreparedLaunchStore(broker).load(path)
    units: list[str] = []
    monkeypatch.setattr(
        ManagedExecutor,
        "runtime_proof",
        staticmethod(lambda unit: units.append(unit) or (True, True)),
    )

    recovered = ManagedExecutor(broker).recover_execution(
        path, observed_monotonic=expires
    )

    assert recovered == {"state": "released", "reason": "cancelled"}
    assert units == [manifest.unit]
    assert broker.status()["leases"]["managed-parent"]["state"] == "cancelled"


def test_lost_executor_recovery_holds_unexpired_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker, path = prepared(tmp_path)
    _, expires = persist_execution_receipt(broker, path, "starting")
    monkeypatch.setattr(
        ManagedExecutor, "runtime_proof", staticmethod(lambda _unit: (True, True))
    )

    recovered = ManagedExecutor(broker).recover_execution(
        path, observed_monotonic=expires - 0.001
    )

    assert recovered == {"state": "unknown", "reason": "runtime_ambiguous"}
    assert broker.lease("managed-parent")["state"] == "execution_active"


@pytest.mark.parametrize("proof", [(True, None), (False, True), (None, None)])
def test_lost_executor_recovery_holds_ambiguous_runtime_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proof: tuple[bool | None, bool | None],
) -> None:
    broker, path = prepared(tmp_path)
    _, expires = persist_execution_receipt(broker, path, "starting")
    monkeypatch.setattr(ManagedExecutor, "runtime_proof", staticmethod(lambda _unit: proof))

    recovered = ManagedExecutor(broker).recover_execution(
        path, observed_monotonic=expires
    )

    assert recovered == {"state": "unknown", "reason": "runtime_ambiguous"}
    assert broker.lease("managed-parent")["state"] == "execution_active"


def test_blank_control_group_is_not_empty_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(arguments: list[str], **_: object) -> SimpleNamespace:
        value = "inactive\n" if "--property=ActiveState" in arguments else "\n"
        return SimpleNamespace(returncode=0, stdout=value)

    monkeypatch.setattr(subprocess, "run", run)
    assert ManagedExecutor.runtime_proof("unit.service") == (True, None)


def test_missing_cgroup_events_is_not_empty_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(arguments: list[str], **_: object) -> SimpleNamespace:
        value = "inactive\n" if "--property=ActiveState" in arguments else "/gone\n"
        return SimpleNamespace(returncode=0, stdout=value)

    monkeypatch.setattr(subprocess, "run", run)
    assert ManagedExecutor.runtime_proof("unit.service") == (True, None)


def test_managed_parent_launcher_refusal_never_prepares_or_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = Broker(tmp_path / "broker")
    broker.initialize(MANAGED_ROOT_BUDGET)
    called = False

    def prepare(*_args: object, **_kwargs: object) -> Path:
        nonlocal called
        called = True
        raise AssertionError("refused launches must not be prepared")

    monkeypatch.setattr(PreparedLaunchStore, "prepare", prepare)
    result = ManagedParentLauncher(broker).run(
        work_id=WORK_ID,
        grant_id=GRANT_ID,
        grant_version=7,
        control=tmp_path,
        writer=tmp_path,
        codex_home=tmp_path,
        codex_executable=Path("/bin/true"),
        assignment="task",
        pressure=Pressure(16_000, 100, 4_000, 3_900, 0, swap_activity_pages=0),
    )
    assert result["state"] == "refused"
    assert called is False


def test_initial_launch_obtains_bounded_swap_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = Broker(tmp_path / "broker")
    broker.initialize(MANAGED_ROOT_BUDGET)
    samples = iter([
        Pressure(16_000, 12_000, 4_000, 3_900, 0, sampled_monotonic=10, pswpin=5, pswpout=7),
        Pressure(16_000, 12_000, 4_000, 3_900, 0, sampled_monotonic=10.1,
                 pswpin=5, pswpout=7),
    ])
    observed: list[Pressure] = []
    monkeypatch.setattr(Pressure, "current", classmethod(lambda cls: next(samples)))
    monkeypatch.setattr("switchstand.managed_launch.time.sleep", lambda delay: delay == 0.1)
    monkeypatch.setattr(
        broker, "reserve",
        lambda request, pressure: observed.append(pressure) or {
            "request_id": request.request_id, "parent": "root", "state": "refused",
            "reason": "test",
        },
    )
    result = ManagedParentLauncher(broker).run(
        work_id=WORK_ID, grant_id=GRANT_ID, grant_version=7,
        control=tmp_path, writer=tmp_path, codex_home=tmp_path,
        codex_executable=Path("/bin/true"), assignment="task",
    )
    assert result["state"] == "refused"
    assert observed[0].swap_activity_pages == 0
    assert observed[0].activity_window_seconds == pytest.approx(0.1)


def test_managed_launcher_turns_term_and_hup_into_executor_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    broker = Broker(tmp_path / "broker")
    broker.initialize(MANAGED_ROOT_BUDGET)
    for name, mode in (("control", 0o755), ("writer", 0o700), ("home", 0o700)):
        (tmp_path / name).mkdir(mode=mode)
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    seen: list[signal.Signals] = []

    def run(_executor: ManagedExecutor, _manifest: Path) -> dict[str, str]:
        for signum in (signal.SIGTERM, signal.SIGHUP):
            try:
                signal.raise_signal(signum)
            except KeyboardInterrupt:
                seen.append(signum)
        return {"state": "completed"}

    monkeypatch.setattr(ManagedExecutor, "run", run)
    result = ManagedParentLauncher(broker).run(
        work_id=WORK_ID, grant_id=GRANT_ID, grant_version=7,
        control=tmp_path / "control", writer=tmp_path / "writer",
        codex_home=tmp_path / "home", codex_executable=executable,
        assignment="task", pressure=GREEN,
    )
    assert result["state"] == "completed"
    assert seen == [signal.SIGTERM, signal.SIGHUP]


def test_direct_trusted_reservations_are_atomic(tmp_path: Path) -> None:
    broker = Broker(tmp_path / "broker")
    broker.initialize(Budget(700, 1024, 128, 100, 96, 1, 0))
    from switchstand.agent_broker import ChildBudget, LeaseRequest

    empty = ChildBudget(
        memory_high_mib=0, memory_max_mib=0, swap_max_mib=0,
        cpu_percent=0, tasks=0, workers=0, heavy=0,
    )
    first = broker.reserve(LeaseRequest(
        request_id="one", parent="root", worker="one", worker_class="light", children=empty,
    ), GREEN)
    second = broker.reserve(LeaseRequest(
        request_id="two", parent="root", worker="two", worker_class="light", children=empty,
    ), GREEN)
    assert first["state"] == "reserved"
    assert second == {"request_id": "two", "parent": "root", "state": "refused",
                      "reason": "parent_budget"}
