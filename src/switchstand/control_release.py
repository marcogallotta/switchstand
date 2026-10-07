"""Stage and select one exact CONTROL release."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .secure_file import atomic_replace_bytes, read_private_bytes

REPOSITORY = "marcogallotta/switchstand"
REMOTE = f"https://github.com/{REPOSITORY}.git"
GIT = "/usr/bin/git"
MODE = "--_switchstand-check-manifest-v1"
GIT_ENV = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "XDG_CONFIG_HOME": "/nonexistent",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
}


def _git(*arguments: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        [GIT, *arguments], cwd=cwd, env=GIT_ENV, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _remote_main() -> str:
    fields = _git("ls-remote", REMOTE, "refs/heads/main").split()
    if len(fields) != 2 or fields[1] != "refs/heads/main":
        raise RuntimeError("remote main is unavailable")
    return fields[0]


def _validate(selector: Path, prospective: Path, home: Path) -> None:
    subprocess.run(
        [selector, MODE, str(prospective)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        check=True,
    )


def activate(target: str) -> str:
    if len(target) != 40 or any(character not in "0123456789abcdef" for character in target):
        raise ValueError("target must be an exact lowercase SHA")
    if _remote_main() != target:
        raise RuntimeError("target is not current remote main")

    home = Path.home()
    root = home / ".local/state/switchstand/control"
    controls = root / "controls"
    manifest = root / "manifest"
    selector = home / ".local/bin/switchstand-start"
    control = controls / target
    selector_source = Path(__file__).resolve().parents[2] / "scripts/switchstand-selector"
    if selector.read_bytes() != selector_source.read_bytes():
        raise RuntimeError("installed selector does not match this CONTROL release")
    if root.resolve(strict=True) != root or controls.resolve(strict=True) != controls:
        raise RuntimeError("CONTROL root is missing, redirected, or non-canonical")

    if not control.exists():
        staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=controls))
        checkout = staging / "snapshot"
        try:
            _git("clone", "--no-checkout", REMOTE, str(checkout))
            if _git("rev-parse", "refs/remotes/origin/main", cwd=checkout) != target:
                raise RuntimeError("staged remote main moved")
            _git("checkout", "--detach", target, cwd=checkout)
            if _git("rev-parse", "HEAD", cwd=checkout) != target:
                raise RuntimeError("staged HEAD does not match target")
            _git("remote", "set-url", "origin", f"https://github.com/{REPOSITORY}.git", cwd=checkout)
            if control.exists():
                raise RuntimeError("CONTROL destination appeared during staging")
            checkout.rename(control)
            descriptor = os.open(controls, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
            os.fsync(descriptor)
            os.close(descriptor)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    body = (
        f"state=ACTIVE\nrepository={REPOSITORY}\ncontrol_sha={target}\n"
        f"control_path={control}\n"
    ).encode()
    descriptor, name = tempfile.mkstemp(prefix=".prospective-", dir=root)
    prospective = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        _validate(selector, prospective, home)
    finally:
        prospective.unlink(missing_ok=True)

    if _remote_main() != target:
        raise RuntimeError("remote main moved before selection")
    try:
        atomic_replace_bytes(manifest, body)
    except OSError as error:
        try:
            observed = read_private_bytes(manifest)
        except OSError, ValueError:
            raise RuntimeError("UNKNOWN") from error
        state = "UNKNOWN"
        if observed == body:
            state = "APPLIED_WITH_DURABILITY_UNKNOWN"
        raise RuntimeError(state) from error
    try:
        observed = read_private_bytes(manifest)
    except (OSError, ValueError) as error:
        raise RuntimeError("UNKNOWN") from error
    if observed != body:
        raise RuntimeError("UNKNOWN")
    return "APPLIED"


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("usage: python -m switchstand.control_release <target-sha>")
        print(activate(sys.argv[1]))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"CONTROL release: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
