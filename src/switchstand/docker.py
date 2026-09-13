import json
import subprocess
from typing import Any, Literal, NamedTuple, cast

OWNER_LABEL = "com.switchstand.run"
ROLE_LABEL = "com.switchstand.role"
DockerKind = Literal["container", "network"]


class DockerObject(NamedTuple):
    kind: DockerKind
    name: str
    object_id: str
    owner: str | None
    role: str | None


def _docker(arguments: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def inspect_object(kind: DockerKind, name: str, env: dict[str, str]) -> DockerObject | None:
    completed = _docker(["inspect", "--type", kind, name], env)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if "No such" in detail or "not found" in detail:
            return None
        raise RuntimeError(f"docker inspect {kind} {name} failed: {detail or 'no diagnostic output'}")
    try:
        values = cast(list[dict[str, Any]], json.loads(completed.stdout))
        if len(values) != 1:
            raise ValueError("expected exactly one object")
        value = values[0]
        object_id = cast(str, value["Id"])
        raw_name = cast(str, value["Name"])
        if kind == "container":
            config = cast(dict[str, Any], value.get("Config") or {})
            labels = cast(dict[str, str], config.get("Labels") or {})
            object_name = raw_name.removeprefix("/")
        else:
            labels = cast(dict[str, str], value.get("Labels") or {})
            object_name = raw_name
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"docker returned invalid {kind} inspection for {name}") from error
    return DockerObject(
        kind=kind,
        name=object_name,
        object_id=object_id,
        owner=labels.get(OWNER_LABEL),
        role=labels.get(ROLE_LABEL),
    )


def label_arguments(owner: str, role: str) -> list[str]:
    return ["--label", f"{OWNER_LABEL}={owner}", "--label", f"{ROLE_LABEL}={role}"]


def require_absent(
    kind: DockerKind, name: str, owner: str, role: str, env: dict[str, str]
) -> None:
    existing = inspect_object(kind, name, env)
    if existing is None:
        return
    if existing.owner == owner and existing.role == role:
        raise RuntimeError(f"owned Docker {kind} {name!r} already exists")
    raise RuntimeError(f"foreign Docker {kind} {name!r} occupies the managed name")


def remove_owned(
    kind: DockerKind, name: str, owner: str, role: str, env: dict[str, str]
) -> None:
    existing = inspect_object(kind, name, env)
    if existing is None:
        return
    if existing.owner != owner or existing.role != role:
        raise RuntimeError(f"refusing to remove foreign Docker {kind} {name!r}")
    arguments = (
        ["rm", "-f", existing.object_id]
        if kind == "container"
        else ["network", "rm", existing.object_id]
    )
    removed = _docker(arguments, env)
    if removed.returncode != 0:
        detail = (removed.stderr or removed.stdout).strip()
        raise RuntimeError(
            f"docker remove {kind} {name} failed: {detail or 'no diagnostic output'}"
        )
    if inspect_object(kind, name, env) is not None:
        raise RuntimeError(f"Docker {kind} {name!r} still exists after removal")
