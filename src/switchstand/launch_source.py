import argparse
import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .task_ref import asana_task_id

SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
CANDIDATE_REF_PATTERN = re.compile(
    r"refs/(?:heads/[A-Za-z0-9._/-]+|pull/[1-9][0-9]*/head)\Z"
)
MARKERS = (
    "SWITCHSTAND_REPOSITORY",
    "SWITCHSTAND_BASE_REF",
    "SWITCHSTAND_BASE_SHA",
    "SWITCHSTAND_CANDIDATE_REF",
    "SWITCHSTAND_CANDIDATE_SHA",
)
JSON = dict[str, Any]


class LaunchSourceError(ValueError):
    pass


@dataclass(frozen=True)
class LaunchSource:
    repository: str
    base_ref: str
    base_sha: str
    candidate_ref: str
    candidate_sha: str


def parse_notes(notes: str) -> LaunchSource:
    values: dict[str, str] = {}
    for line in notes.splitlines():
        name, separator, value = line.strip().partition("=")
        if name not in MARKERS:
            continue
        if not separator or name in values or not value:
            raise LaunchSourceError(f"invalid or duplicate launch marker {name!r}")
        values[name] = value
    if set(values) != set(MARKERS):
        missing = ", ".join(sorted(set(MARKERS) - set(values)))
        raise LaunchSourceError(f"launch task is missing exact source markers: {missing}")
    repository = values["SWITCHSTAND_REPOSITORY"]
    base_ref = values["SWITCHSTAND_BASE_REF"]
    base_sha = values["SWITCHSTAND_BASE_SHA"]
    candidate_ref = values["SWITCHSTAND_CANDIDATE_REF"]
    candidate_sha = values["SWITCHSTAND_CANDIDATE_SHA"]
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise LaunchSourceError("launch repository must be an exact owner/repository slug")
    if base_ref != "refs/heads/main":
        raise LaunchSourceError("launch base ref must be refs/heads/main")
    if CANDIDATE_REF_PATTERN.fullmatch(candidate_ref) is None:
        raise LaunchSourceError("launch candidate ref is not an allowed exact remote ref")
    for label, value in (("base", base_sha), ("candidate", candidate_sha)):
        if SHA_PATTERN.fullmatch(value) is None:
            raise LaunchSourceError(f"launch {label} SHA must be exact lowercase 40-character hex")
    return LaunchSource(repository, base_ref, base_sha, candidate_ref, candidate_sha)


def _task_notes(task_id: str, token: str) -> str:
    request = urllib.request.Request(
        "https://app.asana.com/api/1.0/tasks/" + task_id + "?opt_fields=gid,notes",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = cast(JSON, json.load(response))
    except (OSError, ValueError, urllib.error.URLError):
        raise LaunchSourceError("exact launch task read failed") from None
    data = payload.get("data")
    if not isinstance(data, dict):
        raise LaunchSourceError("exact launch task response is invalid")
    task = cast(JSON, data)
    notes = task.get("notes")
    if task.get("gid") != task_id or not isinstance(notes, str):
        raise LaunchSourceError("exact launch task response is invalid")
    return notes


def load_asana_token(config: Path) -> str:
    try:
        lines = config.read_text().splitlines()
    except OSError:
        raise LaunchSourceError("protected Asana config is unavailable") from None
    tokens: list[str] = []
    for line in lines:
        name, separator, value = line.partition("=")
        if separator and name.strip() == "ASANA_TOKEN":
            tokens.append(value.strip())
    if len(tokens) != 1 or not tokens[0]:
        raise LaunchSourceError("ASANA_TOKEN is missing or empty in protected Asana config")
    token = tokens[0]
    if token.startswith(("'", '"')) or any(character.isspace() for character in token):
        raise LaunchSourceError("ASANA_TOKEN must be a plain unquoted value in protected Asana config")
    return token


def _git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=repo, check=check, text=True, capture_output=True
    )


def _origin_repository(url: str) -> str:
    value = url.strip()
    prefixes = (
        "https://github.com/",
        "ssh://git@github.com/",
        "git@github.com:",
    )
    path = next((value[len(prefix):] for prefix in prefixes if value.startswith(prefix)), None)
    if path is None:
        raise LaunchSourceError("origin must be a github.com repository")
    path = path.removesuffix(".git")
    if REPOSITORY_PATTERN.fullmatch(path) is None:
        raise LaunchSourceError("origin is not an exact github.com owner/repository")
    return path


def _remote_sha(repo: Path, remote_ref: str) -> str:
    completed = _git(repo, "ls-remote", "--refs", "origin", remote_ref)
    lines = [line.split("\t", 1) for line in completed.stdout.splitlines() if line]
    if len(lines) != 1 or len(lines[0]) != 2 or lines[0][1] != remote_ref:
        raise LaunchSourceError(f"remote ref is missing or ambiguous: {remote_ref}")
    sha = lines[0][0]
    if SHA_PATTERN.fullmatch(sha) is None:
        raise LaunchSourceError(f"remote ref returned an invalid SHA: {remote_ref}")
    return sha


def _local_ref(repo: Path, name: str) -> str | None:
    presence = _git(repo, "show-ref", "--verify", "--quiet", name, check=False)
    if presence.returncode == 1:
        return None
    if presence.returncode != 0:
        raise LaunchSourceError(f"cannot inspect prepared ref {name}")
    completed = _git(repo, "show-ref", "--verify", "--hash", name)
    value = completed.stdout.strip()
    if SHA_PATTERN.fullmatch(value) is None:
        raise LaunchSourceError(f"prepared ref has invalid identity: {name}")
    return value


def _fetch_exact_ref(
    repo: Path, task_id: str, label: str, remote_ref: str, expected_sha: str
) -> None:
    remote_sha = _remote_sha(repo, remote_ref)
    if remote_sha != expected_sha:
        raise LaunchSourceError(
            f"launch {label} moved: expected {expected_sha}, remote {remote_sha}"
        )
    local_ref = f"refs/switchstand/launch/{task_id}/{label}"
    current = _local_ref(repo, local_ref)
    if current is not None and current != expected_sha:
        raise LaunchSourceError(f"prepared launch {label} ref is stale: {local_ref}")
    if current is None:
        try:
            _git(
                repo,
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "--no-write-fetch-head",
                "origin",
                f"{remote_ref}:{local_ref}",
            )
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout).strip()
            raise LaunchSourceError(
                f"cannot fetch exact launch {label}: {detail or 'git fetch failed'}"
            ) from None
    if _local_ref(repo, local_ref) != expected_sha:
        raise LaunchSourceError(f"prepared launch {label} did not retain exact identity")
    try:
        _git(repo, "cat-file", "-e", f"{expected_sha}^{{commit}}")
    except subprocess.CalledProcessError:
        raise LaunchSourceError(f"launch {label} is not an available commit") from None


def prepare_source(
    repo: Path, task_id: str, source: LaunchSource, control_sha: str
) -> LaunchSource:
    if SHA_PATTERN.fullmatch(control_sha) is None:
        raise LaunchSourceError("selected CONTROL SHA must be exact lowercase 40-character hex")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if head != control_sha:
        raise LaunchSourceError("launch resolver is not executing from the selected CONTROL SHA")
    if source.base_sha != control_sha:
        raise LaunchSourceError("launch task base does not match the selected CONTROL SHA")
    origin = _git(repo, "remote", "get-url", "origin").stdout.strip()
    if _origin_repository(origin) != source.repository:
        raise LaunchSourceError("launch task repository does not match this checkout origin")
    _fetch_exact_ref(repo, task_id, "base", source.base_ref, source.base_sha)
    _fetch_exact_ref(repo, task_id, "candidate", source.candidate_ref, source.candidate_sha)
    if _git(
        repo, "merge-base", "--is-ancestor", source.base_sha, source.candidate_sha, check=False
    ).returncode != 0:
        raise LaunchSourceError("launch candidate is not based on the accepted base")
    return source


def resolve(repo: Path, active: str, token: str, control_sha: str) -> LaunchSource:
    task_id = asana_task_id(active)
    return prepare_source(repo, task_id, parse_notes(_task_notes(task_id, token)), control_sha)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Resolve and prepare an exact managed launch source.")
    result.add_argument("--repo", type=Path, required=True)
    result.add_argument("--control-sha", required=True)
    result.add_argument("active", help="exact active Asana task ID or URL")
    return result


def main() -> None:
    arguments = parser().parse_args()
    try:
        token = load_asana_token(Path.home() / ".config" / "switchstand" / ".env")
        source = resolve(
            arguments.repo.resolve(strict=True), arguments.active, token, arguments.control_sha
        )
    except (KeyError, LaunchSourceError, OSError, subprocess.CalledProcessError) as error:
        parser().exit(1, f"launch source preparation failed: {error}\n")
    print(source.base_sha, source.candidate_sha)


if __name__ == "__main__":
    main()
