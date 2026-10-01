import io
import json
import stat
import subprocess
import tarfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from switchstand import rehearsal_target as target
from switchstand.edge_maintenance import Failed

SHA = "a" * 40


class Docker:
    def __init__(self, runtime: Path, *, fault: str = ""):
        self.runtime = runtime
        self.fault = fault
        self.commands: list[list[str]] = []
        self.project = "switchstand-rehearsal-proof"
        self.container = "c" * 64
        self.container_present = True
        self.subnet = target.development_subnet(target.REHEARSALS, "proof")

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        output = ""
        if self.fault == "network_create" and command[:3] == ["docker", "network", "create"]:
            detail = "could not find an available, non-overlapping IPv4 address pool\x00\x7f\x85\u202e" + (
                "x" * target.FAILURE_DETAIL_LIMIT
            )
            raise subprocess.CalledProcessError(1, command, stderr=detail)
        if self.fault == "restore" and "pg_restore" in command:
            raise subprocess.CalledProcessError(7, command, stderr="synthetic restore failure")
        if self.fault == "remove_volume" and command[:3] == ["docker", "volume", "rm"]:
            raise subprocess.CalledProcessError(8, command, stderr="synthetic removal failure")
        if command[:3] == ["docker", "rm", "-f"]:
            self.container_present = False
        if command[:4] == ["git", "-C", str(self.runtime), "rev-parse"]:
            output = SHA + "\n"
        elif command[-3:] == ["ps", "-q", "postgres"]:
            output = self.container + "\n"
        elif command[:2] == ["docker", "inspect"]:
            networks = {f"{self.project}_default": {}}
            mounts = [{"Type": "volume", "Name": f"{self.project}_postgres-data",
                       "Destination": "/var/lib/postgresql"}]
            if self.fault == "network": networks["switchstand_default"] = {}
            if self.fault == "volume": mounts.append(
                {"Type": "volume", "Name": "switchstand_postgres-data", "Destination": "/prod"}
            )
            output = json.dumps([{
                "Config": {"Labels": {
                    "com.docker.compose.project": self.project,
                    "com.docker.compose.service": "postgres",
                }},
                "NetworkSettings": {"Networks": networks}, "Mounts": mounts,
                "State": {"Running": self.fault != "stopped", "Health": {"Status": "healthy"}},
            }])
        elif command[:3] == ["docker", "ps", "-aq"]:
            if self.fault != "absent" and self.container_present:
                output = (self.container if "--no-trunc" in command else self.container[:12]) + "\n"
        elif command[:3] in (["docker", "volume", "ls"], ["docker", "network", "ls"]):
            if command[1] == "network" and "-q" in command:
                output = "n" * 64 + "\n"
            elif self.fault == "network_collision" and command[1] == "network":
                if command[-1].startswith("name="):
                    output = f"{self.project}_default\n"
            elif self.fault != "absent":
                suffix = "postgres-data" if command[1] == "volume" else "default"
                output = f"{self.project}_{suffix}\n"
        elif command[:3] in (["docker", "volume", "inspect"], ["docker", "network", "inspect"]):
            suffix = "postgres-data" if command[1] == "volume" else "default"
            label = "production" if self.fault == "label" else suffix
            value = {"Name": f"{self.project}_{suffix}", "Labels": {
                "com.docker.compose.project": self.project,
                f"com.docker.compose.{command[1]}": label,
            }}
            if command[1] == "network":
                value["IPAM"] = {"Config": [{
                    "Subnet": "10.255.255.0/24" if self.fault == "subnet" else self.subnet,
                    "Gateway": self.subnet.removesuffix("0/24") + "1",
                }]}
            output = json.dumps([value])
        return subprocess.CompletedProcess(command, 0, output, "")


def artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rehearsals = tmp_path / "rehearsals"
    rehearsals.mkdir(mode=0o700)
    monkeypatch.setattr(target, "REHEARSALS", rehearsals)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "compose.state.yaml").write_text("services: {}\n")
    backup = tmp_path / "source.dump"
    backup.write_bytes(b"postgres-copy")
    backup.chmod(0o600)
    snapshot = tmp_path / "fastmcp.tar"
    with tarfile.open(snapshot, "w") as archive:
        value = b"oauth-copy"
        info = tarfile.TarInfo("fastmcp/oauth.json")
        info.size = len(value)
        archive.addfile(info, io.BytesIO(value))
    snapshot.chmod(0o600)
    return runtime, backup, snapshot


def test_provision_binds_and_populates_only_namespaced_target(tmp_path, monkeypatch):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    docker = Docker(runtime)

    descriptor = target.provision("proof", SHA, runtime, backup, snapshot, run=docker)
    value = json.loads(descriptor.read_text())

    assert stat.S_IMODE(descriptor.stat().st_mode) == 0o600
    assert value["status"] == "READY" and value["candidate_sha"] == SHA
    assert len(value["container"]) == 64
    assert value["project"] == "switchstand-rehearsal-proof"
    assert value["volume"] == "switchstand-rehearsal-proof_postgres-data"
    assert value["network"] == "switchstand-rehearsal-proof_default"
    assert value["subnet"] == target.development_subnet(target.REHEARSALS, "proof")
    assert len(set(value["endpoints"].values())) == 3
    assert all(item.startswith("http://127.0.0.1:") for item in value["endpoints"].values())
    assert Path(value["copied_database_backup"]).read_bytes() == b"postgres-copy"
    copied = descriptor.parent / "copied-state/fastmcp"
    assert copied.is_dir() and stat.S_IMODE(copied.stat().st_mode) == 0o700
    assert (copied / "oauth.json").read_bytes() == b"oauth-copy"
    assert value["database_backup_sha256"] != value["fastmcp_snapshot_sha256"]
    network_create = [
        "docker", "network", "create", "--subnet", value["subnet"],
        "--label", f"com.docker.compose.project={value['project']}",
        "--label", "com.docker.compose.network=default", value["network"],
    ]
    assert network_create in docker.commands
    assert docker.commands.index(network_create) < next(
        index for index, command in enumerate(docker.commands)
        if command[-4:] == ["up", "-d", "--wait", "postgres"]
    )
    assert any(command[:2] == ["docker", "cp"] for command in docker.commands)
    assert not any("switchstand_postgres-data" in part for command in docker.commands for part in command)
    root, ready = target.load_ready("proof", SHA, runtime, run=docker)
    assert root == descriptor.parent and ready["container"] == docker.container

    target.teardown("proof", run=docker)
    assert not descriptor.parent.exists()
    assert ["docker", "volume", "rm", value["volume"]] in docker.commands
    assert ["docker", "network", "rm", value["network"]] in docker.commands


def test_provision_refuses_production_identity_before_effects(tmp_path, monkeypatch):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    docker = Docker(runtime)

    with pytest.raises(Failed, match="name is invalid"):
        target.provision("production", SHA, runtime, backup, snapshot, run=docker)

    assert docker.commands == []


def test_failed_provision_records_bounded_command_evidence_and_zero_resource_cleanup(
    tmp_path, monkeypatch,
):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    failed = Docker(runtime, fault="network_create")

    with pytest.raises(Failed, match="failed at network_create"):
        target.provision("proof", SHA, runtime, backup, snapshot, run=failed)

    descriptor = target.REHEARSALS / "proof/target.json"
    value = json.loads(descriptor.read_text())
    assert value["status"] == "FAILED"
    assert value["failure"]["step"] == "network_create"
    assert value["failure"]["kind"] == "CalledProcessError"
    assert value["failure"]["exit_code"] == 1
    assert len(value["failure"]["stderr"]) == target.FAILURE_DETAIL_LIMIT
    assert not any(item in value["failure"]["stderr"] for item in ("\x00", "\x7f", "\x85", "\u202e"))
    assert value["failure"]["stderr"].startswith(
        "could not find an available, non-overlapping IPv4 address pool????"
    )

    absent = Docker(runtime, fault="absent")
    target.teardown("proof", run=absent)

    assert not descriptor.parent.exists()
    assert not any(command[:2] == ["docker", "rm"] for command in absent.commands)
    assert not any(command[2:3] == ["rm"] for command in absent.commands)


def test_failed_provision_refuses_foreign_network_name_collision(tmp_path, monkeypatch):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    failed = Docker(runtime, fault="network_create")
    with pytest.raises(Failed):
        target.provision("proof", SHA, runtime, backup, snapshot, run=failed)
    observed = Docker(runtime, fault="network_collision")

    with pytest.raises(Failed, match="does not own exact network inventory"):
        target.teardown("proof", run=observed)

    assert (target.REHEARSALS / "proof/target.json").exists()
    assert not any(command[2:3] == ["rm"] for command in observed.commands)


def test_failed_provision_cleanup_requires_and_removes_exact_partial_resources(
    tmp_path, monkeypatch,
):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    docker = Docker(runtime, fault="restore")

    with pytest.raises(Failed, match="failed at database_restore"):
        target.provision("proof", SHA, runtime, backup, snapshot, run=docker)

    descriptor = target.REHEARSALS / "proof/target.json"
    value = json.loads(descriptor.read_text())
    assert value["status"] == "FAILED" and value["container"] == docker.container
    assert value["failure"] == {
        "step": "database_restore", "kind": "CalledProcessError",
        "exit_code": 7, "stderr": "synthetic restore failure",
    }
    docker.fault = ""
    target.teardown("proof", run=docker)

    assert not descriptor.parent.exists()
    assert ["docker", "rm", "-f", docker.container] in docker.commands
    assert ["docker", "volume", "rm", f"{docker.project}_postgres-data"] in docker.commands
    assert ["docker", "network", "rm", f"{docker.project}_default"] in docker.commands


def test_failed_provision_cleanup_allows_bound_container_to_be_absent(tmp_path, monkeypatch):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    docker = Docker(runtime, fault="restore")
    with pytest.raises(Failed):
        target.provision("proof", SHA, runtime, backup, snapshot, run=docker)
    docker.fault = ""
    docker.container_present = False

    target.teardown("proof", run=docker)

    assert not (target.REHEARSALS / "proof").exists()
    assert ["docker", "volume", "rm", f"{docker.project}_postgres-data"] in docker.commands
    assert ["docker", "network", "rm", f"{docker.project}_default"] in docker.commands


def test_failed_provision_cleanup_retries_after_container_only_removal(tmp_path, monkeypatch):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    docker = Docker(runtime, fault="restore")
    with pytest.raises(Failed):
        target.provision("proof", SHA, runtime, backup, snapshot, run=docker)
    docker.fault = "remove_volume"

    with pytest.raises(Failed, match="teardown could not prove ownership"):
        target.teardown("proof", run=docker)

    assert docker.container_present is False
    assert (target.REHEARSALS / "proof/target.json").exists()
    docker.fault = ""
    target.teardown("proof", run=docker)
    assert not (target.REHEARSALS / "proof").exists()


def test_failed_provision_cleanup_refuses_foreign_resource_labels(tmp_path, monkeypatch):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    setup = Docker(runtime, fault="restore")
    with pytest.raises(Failed):
        target.provision("proof", SHA, runtime, backup, snapshot, run=setup)
    observed = Docker(runtime, fault="label")

    with pytest.raises(Failed, match="does not own"):
        target.teardown("proof", run=observed)

    assert (target.REHEARSALS / "proof/target.json").exists()
    assert not any(command[:2] == ["docker", "rm"] for command in observed.commands)


@pytest.mark.parametrize("key", ["candidate_sha", "copied_database_backup"])
def test_load_ready_refuses_candidate_or_copied_state_drift(tmp_path, monkeypatch, key):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    docker = Docker(runtime)
    descriptor = target.provision("proof", SHA, runtime, backup, snapshot, run=docker)
    value = json.loads(descriptor.read_text())
    value[key] = "0" * 40 if key == "candidate_sha" else str(tmp_path / "foreign")
    descriptor.write_text(json.dumps(value))
    descriptor.chmod(0o600)
    docker.commands.clear()

    with pytest.raises(Failed, match="candidate or copied state"):
        target.load_ready("proof", SHA, runtime, run=docker)

    assert docker.commands == []


@pytest.mark.parametrize("fault", ["endpoints", "container", "stopped", "tree", "subnet"])
def test_load_ready_refuses_descriptor_or_live_container_drift(tmp_path, monkeypatch, fault):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    setup = Docker(runtime)
    descriptor = target.provision("proof", SHA, runtime, backup, snapshot, run=setup)
    if fault == "endpoints":
        value = json.loads(descriptor.read_text())
        value["endpoints"]["public"] = value["endpoints"]["caddy"]
        descriptor.write_text(json.dumps(value))
        descriptor.chmod(0o600)
    observed = Docker(runtime)
    if fault == "container":
        observed.container = "d" * 64
    elif fault == "stopped":
        observed.fault = "stopped"
    elif fault == "tree":
        (descriptor.parent / "copied-state/fastmcp/oauth.json").write_bytes(b"drift")
    elif fault == "subnet":
        observed.fault = "subnet"

    with pytest.raises(Failed):
        target.load_ready("proof", SHA, runtime, run=observed)


def test_teardown_holds_shared_operator_lock(tmp_path, monkeypatch):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    docker = Docker(runtime)
    target.provision("proof", SHA, runtime, backup, snapshot, run=docker)
    events: list[str] = []

    @contextmanager
    def locked(_path):
        events.append("locked")
        yield
        events.append("released")

    monkeypatch.setattr(target, "_exclusive_lock", locked)
    monkeypatch.setattr(target, "_teardown", lambda *_args, **_kwargs: events.append("teardown"))
    target.teardown("proof", run=docker)
    assert events == ["locked", "teardown", "released"]


@pytest.mark.parametrize(("key", "changed"), [
    ("project", "switchstand"), ("status", "PROVISIONING"), ("container", "other")])
def test_teardown_refuses_foreign_descriptor_before_removal(tmp_path, monkeypatch, key, changed):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    docker = Docker(runtime)
    descriptor = target.provision("proof", SHA, runtime, backup, snapshot, run=docker)
    value = json.loads(descriptor.read_text())
    value[key] = changed
    descriptor.write_text(json.dumps(value))
    descriptor.chmod(0o600)
    docker.commands.clear()

    with pytest.raises(Failed):
        target.teardown("proof", run=docker)

    assert descriptor.exists() and all("rm" not in command[1:3] for command in docker.commands)


@pytest.mark.parametrize("fault", ["network", "volume", "label", "subnet"])
def test_teardown_refuses_foreign_resources_before_any_removal(tmp_path, monkeypatch, fault):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    setup = Docker(runtime)
    descriptor = target.provision("proof", SHA, runtime, backup, snapshot, run=setup)
    docker = Docker(runtime, fault=fault)

    with pytest.raises(Failed):
        target.teardown("proof", run=docker)

    assert descriptor.exists()
    assert all("rm" not in command[1:3] for command in docker.commands)


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_provision_refuses_non_directory_fastmcp_root(tmp_path, monkeypatch, kind):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    with tarfile.open(snapshot, "w") as archive:
        info = tarfile.TarInfo("fastmcp")
        if kind == "symlink":
            info.type, info.linkname = tarfile.SYMTYPE, "/tmp"
            archive.addfile(info)
        else:
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    snapshot.chmod(0o600)
    docker = Docker(runtime)

    with pytest.raises(Failed):
        target.provision("proof", SHA, runtime, backup, snapshot, run=docker)

    assert not any(command[:2] == ["docker", "compose"] for command in docker.commands)
