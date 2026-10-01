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
    def __init__(self, runtime: Path, *, foreign_volume: bool = False):
        self.runtime = runtime
        self.foreign_volume = foreign_volume
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
            output = json.dumps([{
                "Config": {"Labels": {
                    "com.docker.compose.project": self.project,
                    "com.docker.compose.service": "postgres",
                }},
                "NetworkSettings": {"Networks": {f"{self.project}_default": {}}},
                "Mounts": [{"Name": f"{self.project}_postgres-data"}],
            }])
        elif command[:3] == ["docker", "ps", "-aq"]:
            output = "copied-postgres\n"
        elif command[:3] in (["docker", "volume", "ls"], ["docker", "network", "ls"]):
            suffix = "postgres-data" if command[1] == "volume" else "default"
            output = f"{self.project}_{suffix}\n"
            if self.foreign_volume and command[1] == "volume":
                output += "switchstand_postgres-data\n"
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
    assert (descriptor.parent / "copied-state/fastmcp/oauth.json").read_bytes() == b"oauth-copy"
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


def test_teardown_refuses_foreign_descriptor_before_docker(tmp_path, monkeypatch):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    docker = Docker(runtime)
    descriptor = target.provision("proof", SHA, runtime, backup, snapshot, run=docker)
    value = json.loads(descriptor.read_text())
    value["project"] = "switchstand"
    descriptor.write_text(json.dumps(value))
    descriptor.chmod(0o600)
    docker.commands.clear()

    with pytest.raises(Failed, match="foreign identity"):
        target.teardown("proof", run=docker)

    assert docker.commands == [] and descriptor.exists()


def test_teardown_retains_root_when_resource_label_is_foreign(tmp_path, monkeypatch):
    runtime, backup, snapshot = artifacts(tmp_path, monkeypatch)
    setup = Docker(runtime)
    descriptor = target.provision("proof", SHA, runtime, backup, snapshot, run=setup)
    docker = Docker(runtime, foreign_volume=True)

    with pytest.raises(Failed, match="does not own every volume"):
        target.teardown("proof", run=docker)

    assert descriptor.exists()
    assert not any(command[:3] == ["docker", "volume", "rm"] for command in docker.commands)
