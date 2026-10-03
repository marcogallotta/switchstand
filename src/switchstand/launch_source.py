import argparse
import asyncio
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import create_async_engine

from .candidate import CandidateError, prepare_launch_source
from .canonical_work import CanonicalWorkRepository, CurrentWork
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


class LaunchSourceError(ValueError):
    pass


@dataclass(frozen=True)
class LaunchSource:
    repository: str
    base_ref: str
    base_sha: str
    candidate_ref: str
    candidate_sha: str


def repository_marker(notes: str) -> str:
    values = [line.strip().partition("=") for line in notes.splitlines()
              if line.strip().partition("=")[0] == "SWITCHSTAND_REPOSITORY"]
    if (len(values) != 1 or values[0][1] != "="
            or REPOSITORY_PATTERN.fullmatch(values[0][2]) is None):
        raise LaunchSourceError("require exactly one valid SWITCHSTAND_REPOSITORY marker")
    return values[0][2]


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


async def canonical_work(works: CanonicalWorkRepository, reference: str) -> CurrentWork:
    try:
        work_id = UUID(reference)
    except ValueError:
        work_id = await works.resolve_asana_gid(asana_task_id(reference))
    work = None if work_id is None else await works.get(work_id)
    if work is None:
        raise LaunchSourceError("exact launch work read failed")
    return work


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


def prepare_source(
    repo: Path, task_id: str, source: LaunchSource, control_sha: str,
) -> LaunchSource:
    try:
        prepare_launch_source(
            repo,
            task_id,
            repository=source.repository,
            base_ref=source.base_ref,
            base_sha=source.base_sha,
            candidate_ref=source.candidate_ref,
            candidate_sha=source.candidate_sha,
            control_sha=control_sha,
        )
    except CandidateError as error:
        raise LaunchSourceError(str(error)) from None
    return source


async def _resolve(repo: Path, active: str, database_url: str, control_sha: str) -> LaunchSource:
    engine = create_async_engine(database_url)
    try:
        work = await canonical_work(CanonicalWorkRepository(engine), active)
        return prepare_source(repo, str(work.work_id), parse_notes(work.notes), control_sha)
    finally:
        await engine.dispose()


def resolve(repo: Path, active: str, database_url: str, control_sha: str) -> LaunchSource:
    return asyncio.run(_resolve(repo, active, database_url, control_sha))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Resolve and prepare an exact managed launch source.")
    result.add_argument("--repo", type=Path, required=True)
    result.add_argument("--control-sha", required=True)
    result.add_argument("active", help="exact active WorkId or legacy task ID/URL")
    return result


def main() -> None:
    arguments = parser().parse_args()
    try:
        source = resolve(
            arguments.repo.resolve(strict=True), arguments.active,
            os.environ["DATABASE_URL"], arguments.control_sha,
        )
    except (KeyError, LaunchSourceError, OSError, subprocess.CalledProcessError) as error:
        parser().exit(1, f"launch source preparation failed: {error}\n")
    print(source.base_sha, source.candidate_sha)


if __name__ == "__main__":
    main()
