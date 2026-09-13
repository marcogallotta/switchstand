import json
import subprocess
from typing import Any, Literal, NamedTuple, cast

OWNER_LABEL = "com.switchstand.run"
ROLE_LABEL = "com.switchstand.role"
CONTROL_SECONDS = 5
DockerKind = Literal["container", "network"]


class DockerObject(NamedTuple):
    kind: DockerKind
    name: str
    object_id: str
    owner: str | None
    role: str | None
    running: bool | None = None
    exit_code: int | None = None
    status: str | None = None


def _docker(arguments: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *arguments],
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=CONTROL_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"docker {' '.join(arguments)} exceeded {CONTROL_SECONDS} seconds"
        ) from error


def inspect_object(kind: DockerKind, reference: str, env: dict[str, str]) -> DockerObject | None:
    completed = _docker(["inspect", "--type", kind, reference], env)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if "No such" in detail or "not found" in detail:
            return None
        raise RuntimeError(
            f"docker inspect {kind} {reference} failed: {detail or 'no diagnostic output'}"
        )
    try:
        values = cast(list[dict[str, Any]], json.loads(completed.stdout))
        if len(values) != 1:
            raise ValueError("expected exactly one object")
        value = values[0]
        object_id = cast(str, value["Id"])
        raw_name = cast(str, value["Name"])
        running: bool | None = None
        exit_code: int | None = None
        status: str | None = None
        if kind == "container":
            config = cast(dict[str, Any], value.get("Config") or {})
            labels = cast(dict[str, str], config.get("Labels") or {})
            state = cast(dict[str, Any], value.get("State") or {})
            if "Running" in state:
                running = cast(bool, state["Running"])
            if "ExitCode" in state:
                exit_code = cast(int, state["ExitCode"])
            if "Status" in state:
                status = cast(str, state["Status"])
            object_name = raw_name.removeprefix("/")
        else:
            labels = cast(dict[str, str], value.get("Labels") or {})
            object_name = raw_name
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"docker returned invalid {kind} inspection for {reference}") from error
    return DockerObject(
        kind=kind,
        name=object_name,
        object_id=object_id,
        owner=labels.get(OWNER_LABEL),
        role=labels.get(ROLE_LABEL),
        running=running,
        exit_code=exit_code,
        status=status,
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


def remove_exact(
    kind: DockerKind, object_id: str, owner: str, role: str, env: dict[str, str]
) -> None:
    existing = inspect_object(kind, object_id, env)
    if existing is None:
        return
    if existing.object_id != object_id or existing.owner != owner or existing.role != role:
        raise RuntimeError(f"refusing to remove foreign Docker {kind} identity {object_id!r}")
    arguments = (
        ["rm", "-f", object_id] if kind == "container" else ["network", "rm", object_id]
    )
    removed = _docker(arguments, env)
    if removed.returncode != 0:
        detail = (removed.stderr or removed.stdout).strip()
        raise RuntimeError(
            f"docker remove {kind} {object_id} failed: {detail or 'no diagnostic output'}"
        )
    if inspect_object(kind, object_id, env) is not None:
        raise RuntimeError(f"Docker {kind} identity {object_id!r} still exists after removal")
