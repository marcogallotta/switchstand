import json
import subprocess

import pytest

from switchstand import docker


def completed(arguments, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(arguments, returncode, stdout=stdout, stderr=stderr)


def inspection(object_id, name, owner, role):
    return json.dumps([
        {
            "Id": object_id,
            "Name": name,
            "Config": {
                "Labels": {docker.OWNER_LABEL: owner, docker.ROLE_LABEL: role}
            },
        }
    ])


def test_foreign_collision_is_preserved(monkeypatch):
    calls = []

    def command(arguments, env, **kwargs):
        calls.append(arguments)
        return completed(arguments, inspection("foreign-id", "/claimed", "other", "database"))

    monkeypatch.setattr(docker, "command", command)
    with pytest.raises(RuntimeError, match="name collision"):
        docker.require_absent("container", "claimed", {})
    assert calls == [["inspect", "--type", "container", "claimed"]]


def test_cleanup_requires_labels_and_removes_exact_id_with_readback(monkeypatch):
    calls = []
    answers = iter(
        (
            completed([], inspection("owned-id", "/claimed", "run-1", "database")),
            completed([], stdout="owned-id\n"),
            completed([], stderr="No such container", returncode=1),
        )
    )

    def command(arguments, env, **kwargs):
        calls.append(arguments)
        return next(answers)

    monkeypatch.setattr(docker, "command", command)
    docker.remove_owned("container", "owned-id", "run-1", "database", {})
    assert calls == [
        ["inspect", "--type", "container", "owned-id"],
        ["rm", "-f", "owned-id"],
        ["inspect", "--type", "container", "owned-id"],
    ]


def test_cleanup_refuses_foreign_identity(monkeypatch):
    monkeypatch.setattr(
        docker,
        "command",
        lambda arguments, env, **kwargs: completed(
            arguments, inspection("foreign-id", "/claimed", "other", "database")
        ),
    )
    with pytest.raises(RuntimeError, match="foreign Docker"):
        docker.remove_owned("container", "foreign-id", "run-1", "database", {})


def test_control_command_has_a_bounded_timeout(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        docker.command(["inspect", "object"], {}, timeout=0.01)


def test_image_inspection_does_not_require_container_name(monkeypatch):
    value = json.dumps([
        {
            "Id": "sha256:owned",
            "RepoTags": ["switchstand-runner:latest"],
            "Config": {
                "Labels": {docker.OWNER_LABEL: "run-1", docker.ROLE_LABEL: "runner"}
            },
        }
    ])
    monkeypatch.setattr(
        docker,
        "command",
        lambda arguments, env, **kwargs: completed(arguments, stdout=value),
    )
    inspected = docker.inspect("image", "sha256:owned", {})
    assert inspected is not None
    assert inspected.name == "switchstand-runner:latest"
    assert inspected.object_id == "sha256:owned"
