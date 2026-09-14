import json
import subprocess
from typing import Any, Literal, NamedTuple, cast

OWNER_LABEL = "com.switchstand.run"
ROLE_LABEL = "com.switchstand.role"
CONTROL_SECONDS = 10
DockerKind = Literal["container", "image", "network"]


class DockerObject(NamedTuple):
    kind: DockerKind
    name: str
    object_id: str
    owner: str | None
    role: str | None


def command(
    arguments: list[str],
    env: dict[str, str],
    *,
    cwd: str | None = None,
    timeout: float = CONTROL_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *arguments], cwd=cwd, env=env, text=True,
            capture_output=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"docker {' '.join(arguments)} timed out") from error


def inspect(kind: DockerKind, reference: str, env: dict[str, str]) -> DockerObject | None:
    result = command(["inspect", "--type", kind, reference], env)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if "No such" in detail or "not found" in detail:
            return None
        raise RuntimeError(f"docker inspect failed: {detail or 'no diagnostic output'}")
    try:
        values = cast(list[dict[str, Any]], json.loads(result.stdout))
        if len(values) != 1:
            raise ValueError
        value = values[0]
        labels = cast(
            dict[str, str],
            (
                cast(dict[str, Any], value.get("Config") or {}).get("Labels")
                if kind in {"container", "image"}
                else value.get("Labels")
            )
            or {},
        )
        if kind == "image":
            tags = cast(list[str], value.get("RepoTags") or [])
            name = tags[0] if tags else reference
        else:
            name = cast(str, value["Name"]).removeprefix("/")
        object_id = cast(str, value["Id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("docker returned invalid object inspection") from error
    return DockerObject(kind, name, object_id, labels.get(OWNER_LABEL), labels.get(ROLE_LABEL))


def labels(owner: str, role: str) -> list[str]:
    return ["--label", f"{OWNER_LABEL}={owner}", "--label", f"{ROLE_LABEL}={role}"]


def owned_name(owner: str, role: str) -> str:
    return f"switchstand-{role}-{owner}"


def require_absent(kind: DockerKind, name: str, env: dict[str, str]) -> None:
    if inspect(kind, name, env) is not None:
        raise RuntimeError(f"Docker {kind} name collision: {name!r}")


def require_owned(
    kind: DockerKind, reference: str, owner: str, role: str, env: dict[str, str]
) -> DockerObject:
    value = inspect(kind, reference, env)
    if value is None or value.owner != owner or value.role != role:
        raise RuntimeError(f"Docker {kind} is not the exact owned {role} object")
    return value


def remove_owned(
    kind: DockerKind, object_id: str, owner: str, role: str, env: dict[str, str]
) -> None:
    value = inspect(kind, object_id, env)
    if value is None:
        return
    if value.object_id != object_id or value.owner != owner or value.role != role:
        raise RuntimeError(f"refusing to remove foreign Docker {kind} identity")
    if kind == "container":
        arguments = ["rm", "-f", object_id]
    elif kind == "image":
        arguments = ["image", "rm", object_id]
    else:
        arguments = ["network", "rm", object_id]
    removed = command(arguments, env)
    if removed.returncode != 0:
        detail = (removed.stderr or removed.stdout).strip()
        raise RuntimeError(f"docker cleanup failed: {detail or 'no diagnostic output'}")
    if inspect(kind, object_id, env) is not None:
        raise RuntimeError(f"Docker {kind} remains after exact cleanup")
