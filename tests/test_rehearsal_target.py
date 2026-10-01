import io
import json
import stat
import subprocess
import tarfile
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

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        output = ""
        if command[:4] == ["git", "-C", str(self.runtime), "rev-parse"]:
            output = SHA + "\n"
        elif command[-3:] == ["ps", "-q", "postgres"]:
            output = "copied-postgres\n"
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
            }])
        elif command[:3] == ["docker", "ps", "-aq"]:
            output = "copied-postgres\n"
        elif command[:3] in (["docker", "volume", "inspect"], ["docker", "network", "inspect"]):
            suffix = "postgres-data" if command[1] == "volume" else "default"
            label = "production" if self.fault == "label" else suffix
            output = json.dumps([{"Name": f"{self.project}_{suffix}", "Labels": {
                "com.docker.compose.project": self.project,
                f"com.docker.compose.{command[1]}": label,
            }}])
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
    assert value["project"] == "switchstand-rehearsal-proof"
    assert value["volume"] == "switchstand-rehearsal-proof_postgres-data"
    assert value["network"] == "switchstand-rehearsal-proof_default"
    assert len(set(value["endpoints"].values())) == 3
    assert all(item.startswith("http://127.0.0.1:") for item in value["endpoints"].values())
    assert Path(value["copied_database_backup"]).read_bytes() == b"postgres-copy"
    copied = descriptor.parent / "copied-state/fastmcp"
    assert copied.is_dir() and stat.S_IMODE(copied.stat().st_mode) == 0o700
    assert (copied / "oauth.json").read_bytes() == b"oauth-copy"
    assert value["database_backup_sha256"] != value["fastmcp_snapshot_sha256"]
    assert any(command[:2] == ["docker", "cp"] for command in docker.commands)
    assert not any("switchstand_postgres-data" in part for command in docker.commands for part in command)

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


@pytest.mark.parametrize("fault", ["network", "volume", "label"])
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
