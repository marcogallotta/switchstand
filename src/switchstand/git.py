import os
import subprocess
from pathlib import Path
from typing import NamedTuple


class GitError(RuntimeError):
    pass


class LandingIdentity(NamedTuple):
    candidate: str
    previous_base: str
    merged: str
    tree: str


def _commit(repo: Path, sha: str) -> tuple[str, str, tuple[str, ...]]:
    if len(sha) != 40 or not set(sha) <= set("0123456789abcdef"):
        raise GitError("Git identity requires an exact 40-character SHA")
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TZ")
           if key in os.environ}
    found = subprocess.run(
        ["git", "show", "-s", "--format=%H%n%T%n%P", sha], cwd=repo.resolve(strict=True),
        env=env, text=True, capture_output=True, check=False,
    )
    values = found.stdout.splitlines()
    if found.returncode or len(values) != 3 or values[0] != sha:
        raise GitError("Git could not prove the requested commit identity")
    return values[0], values[1], tuple(values[2].split())


def reconcile(repo: Path, candidate: str, previous_base: str, merged: str,
              current_base: str) -> LandingIdentity:
    head, base, result, current = (_commit(repo, sha) for sha in
                                   (candidate, previous_base, merged, current_base))
    if result[0] != current[0]:
        raise GitError("merged result is not the current protected base")
    if result[2] != (base[0], head[0]) or result[1] != head[1]:
        raise GitError("landing does not preserve the authoritative H->M identity")
    return LandingIdentity(head[0], base[0], result[0], result[1])
