import json
import subprocess

import pytest

from switchstand import docker


def completed(args=(), stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def inspected(
    name: str,
    object_id: str,
    owner: str,
    role: str,
    *,
    running: bool = False,
    status: str = "created",
    exit_code: int = 0,
) -> str:
    return json.dumps(
        [
            {
                "Id": object_id,
                "Name": f"/{name}",
                "Config": {
                    "Labels": {
                        docker.OWNER_LABEL: owner,
                        docker.ROLE_LABEL: role,
                    }
                },
                "State": {
                    "Running": running,
                    "Status": status,
                    "ExitCode": exit_code,
                },
            }
        ]
    )


def test_foreign_same_name_is_rejected_without_removal(monkeypatch):
    calls = []

    def fake(arguments, env):
        calls.append(arguments)
        return completed(arguments, inspected("work", "foreign-id", "other-run", "check"))

    monkeypatch.setattr(docker, "_docker", fake)
    with pytest.raises(RuntimeError, match="foreign Docker container"):
        docker.require_absent("container", "work", "this-run", "check", {})
    assert calls == [["inspect", "--type", "container", "work"]]


def test_exact_remove_uses_bound_id_and_verifies_that_id_absent(monkeypatch):
    calls = []
    responses = iter(
        [
            completed(stdout=inspected("renamed", "exact-id", "run", "quality")),
            completed(stdout="exact-id\n"),
            completed(returncode=1, stderr="Error: No such object: exact-id"),
        ]
    )

    def fake(arguments, env):
        calls.append(arguments)
        return next(responses)

    monkeypatch.setattr(docker, "_docker", fake)
    docker.remove_exact("container", "exact-id", "run", "quality", {})
    assert calls == [
        ["inspect", "--type", "container", "exact-id"],
        ["rm", "-f", "exact-id"],
        ["inspect", "--type", "container", "exact-id"],
    ]


def test_exact_remove_refuses_foreign_identity(monkeypatch):
    monkeypatch.setattr(
        docker,
        "_docker",
        lambda arguments, env: completed(
            arguments, inspected("work", "foreign-id", "other-run", "quality")
        ),
    )
    with pytest.raises(RuntimeError, match="refusing to remove foreign"):
        docker.remove_exact("container", "foreign-id", "run", "quality", {})


def test_docker_control_timeout_is_bounded(monkeypatch):
    def hanging(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], docker.CONTROL_SECONDS)

    monkeypatch.setattr(subprocess, "run", hanging)
    with pytest.raises(RuntimeError, match="exceeded 5 seconds"):
        docker.inspect_object("container", "work", {})


def test_container_inspection_reads_execution_state(monkeypatch):
    monkeypatch.setattr(
        docker,
        "_docker",
        lambda arguments, env: completed(
            arguments,
            inspected(
                "work",
                "exact-id",
                "run",
                "check",
                running=False,
                status="exited",
                exit_code=7,
            ),
        ),
    )
    result = docker.inspect_object("container", "exact-id", {})
    assert result is not None
    assert result.status == "exited" and result.running is False and result.exit_code == 7
