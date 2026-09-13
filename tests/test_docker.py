import json
import subprocess

import pytest

from switchstand import docker


def completed(args=(), stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def inspected(name: str, object_id: str, owner: str, role: str) -> str:
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


def test_owned_remove_uses_exact_id_and_verifies_absence(monkeypatch):
    calls = []
    responses = iter(
        [
            completed(stdout=inspected("work", "exact-id", "run", "quality")),
            completed(stdout="exact-id\n"),
            completed(returncode=1, stderr="Error: No such object: work"),
        ]
    )

    def fake(arguments, env):
        calls.append(arguments)
        return next(responses)

    monkeypatch.setattr(docker, "_docker", fake)
    docker.remove_owned("container", "work", "run", "quality", {})
    assert calls == [
        ["inspect", "--type", "container", "work"],
        ["rm", "-f", "exact-id"],
        ["inspect", "--type", "container", "work"],
    ]


def test_remove_refuses_foreign_object(monkeypatch):
    monkeypatch.setattr(
        docker,
        "_docker",
        lambda arguments, env: completed(
            arguments, inspected("work", "foreign-id", "other-run", "quality")
        ),
    )
    with pytest.raises(RuntimeError, match="refusing to remove foreign"):
        docker.remove_owned("container", "work", "run", "quality", {})
