import asyncio
import os
import shutil
import subprocess
from uuid import uuid4

import pytest

from switchstand import development
from switchstand.docker import inspect, labels, owned_name


def _real_docker() -> tuple[str, str]:
    if os.getenv("SWITCHSTAND_REAL_DOCKER") != "1":
        pytest.skip(
            "real Docker lifecycle proof runs only in the explicit socket-mounted Quality step"
        )
    docker = shutil.which("docker")
    if docker is None:
        pytest.fail("MISSING_CAPABILITY: Docker CLI is unavailable")
    version = subprocess.run(
        [docker, "version"], text=True, capture_output=True, check=False, timeout=10
    )
    if version.returncode != 0:
        pytest.fail(
            "MISSING_CAPABILITY: Docker daemon is unavailable: "
            + (version.stderr or version.stdout).strip()
        )
    image = os.getenv("SWITCHSTAND_REAL_DOCKER_IMAGE", "").strip()
    if not image:
        pytest.fail("MISSING_CAPABILITY: SWITCHSTAND_REAL_DOCKER_IMAGE is unset")
    image_check = subprocess.run(
        [docker, "image", "inspect", image],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if image_check.returncode != 0:
        pytest.fail(f"MISSING_CAPABILITY: Docker proof image {image!r} is unavailable")
    return docker, image


async def _wait_running(name: str, env: dict[str, str]) -> None:
    for _ in range(100):
        value = await asyncio.to_thread(inspect, "container", name, env)
        if value is not None and value.running:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"real Docker container {name!r} never reached running state")


async def test_real_docker_lifecycle_owner_boundary() -> None:
    docker, image = _real_docker()
    env = development._environment()
    owner = "real-" + uuid4().hex
    role = "focused"
    name = owned_name(owner, role)

    created = await development._create_owned_container(
        [
            "create",
            "--name",
            name,
            *labels(owner, role),
            image,
            "sh",
            "-c",
            "printf created",
        ],
        owner,
        role,
    )
    assert (created.name, created.owner, created.role) == (name, owner, role)
    assert created.object_id
    await development._remove_created(created, env)
    assert inspect("container", created.object_id, env) is None

    result = await development._run_owned_workload(
        [
            "create",
            "--name",
            name,
            *labels(owner, role),
            image,
            "sh",
            "-c",
            "printf boundary-ok; exit 7",
        ],
        owner,
        role,
        20,
    )
    assert result.returncode == 7
    assert result.stdout == "boundary-ok"
    assert inspect("container", name, env) is None

    foreign = await asyncio.create_subprocess_exec(
        docker,
        "create",
        "--name",
        name,
        *labels("foreign-owner", role),
        image,
        "sh",
        "-c",
        "exit 0",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    foreign_stdout, foreign_stderr = await asyncio.wait_for(foreign.communicate(), 10)
    assert foreign.returncode == 0, foreign_stderr.decode(errors="replace")
    foreign_id = foreign_stdout.decode().strip()
    try:
        with pytest.raises(RuntimeError, match="refusing to clean foreign Docker container"):
            await development._cleanup_named_if_owned(name, owner, role, env)
        foreign = inspect("container", foreign_id, env)
        assert foreign is not None
        assert foreign.owner == "foreign-owner"
    finally:
        removed = await asyncio.create_subprocess_exec(
            docker,
            "rm",
            "-f",
            foreign_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, remove_stderr = await asyncio.wait_for(removed.communicate(), 10)
        assert removed.returncode == 0, remove_stderr.decode(errors="replace")


    with pytest.raises(TimeoutError):
        await development._run_owned_workload(
            [
                "create",
                "--name",
                name,
                *labels(owner, role),
                image,
                "sh",
                "-c",
                "sleep 30",
            ],
            owner,
            role,
            1,
        )
    assert inspect("container", name, env) is None

    cancelled = asyncio.create_task(
        development._run_owned_workload(
            [
                "create",
                "--name",
                name,
                *labels(owner, role),
                image,
                "sh",
                "-c",
                "sleep 30",
            ],
            owner,
            role,
            30,
        )
    )
    await _wait_running(name, env)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert inspect("container", name, env) is None
