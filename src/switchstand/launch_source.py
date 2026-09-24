import argparse
import asyncio
import re
from pathlib import Path

from .task_ref import asana_task_id

from .candidate import (
    CANDIDATE_REF_PATTERN,
    REPOSITORY_PATTERN,
    SHA_PATTERN,
    CandidateError,
    LaunchSource,
    prepare_launch_source,
)
from .provider import AsanaProvider
import httpx

MARKERS = (
    "SWITCHSTAND_REPOSITORY",
    "SWITCHSTAND_BASE_REF",
    "SWITCHSTAND_BASE_SHA",
    "SWITCHSTAND_CANDIDATE_REF",
    "SWITCHSTAND_CANDIDATE_SHA",
)


class LaunchSourceError(ValueError):
    pass


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


async def _task_notes(task_id: str, token: str) -> str:
    async with httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0",
        trust_env=False,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        try:
            task = await AsanaProvider(client).source_task(task_id)
        except Exception as error:
            raise LaunchSourceError("exact launch task read failed") from error
    if task is None or not task.canonical:
        raise LaunchSourceError("exact launch task is unavailable or not canonical")
    return task.notes


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


def resolve(repo: Path, active: str, token: str, control_sha: str) -> LaunchSource:
    task_id = asana_task_id(active)
    try:
        notes = asyncio.run(_task_notes(task_id, token))
        return prepare_launch_source(repo, task_id, parse_notes(notes), control_sha)
    except CandidateError as error:
        raise LaunchSourceError(str(error)) from None


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
    except (KeyError, LaunchSourceError, OSError) as error:
        parser().exit(1, f"launch source preparation failed: {error}\n")
    print(source.base_sha, source.candidate_sha)


if __name__ == "__main__":
    main()
