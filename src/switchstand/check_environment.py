"""Writer-owned, locked dependency generations. No provider or database imports."""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

UV_VERSION = "0.12.10"
TOOLS = ("python", "ruff", "pyright", "pytest")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def interpreter(python: Path) -> dict[str, str]:
    result = subprocess.run(
        [str(python), "-I", "-c",
         "import json,sys; print(json.dumps([sys.version, sys.base_prefix]))"],
        capture_output=True, text=True, check=True,
    )
    version, prefix = json.loads(result.stdout)
    if not version.startswith("3.14."):
        raise ValueError("checks require Python 3.14")
    binary = python.resolve(strict=True)
    return {"path": str(binary), "sha256": digest(binary), "version": version, "prefix": prefix}


def root(repo: Path) -> Path:
    return repo / ".switchstand-check"


def pinned_uv(path: Path) -> Path:
    path = path.resolve(strict=True)
    result = subprocess.run([str(path), "--version"], capture_output=True, text=True, check=True)
    if result.stdout.strip().split()[:2] != ["uv", UV_VERSION]:
        raise ValueError(f"checks require CONTROL's pinned uv {UV_VERSION}")
    return path


def specification(repo: Path, python: Path, uv: Path) -> dict[str, object]:
    return {"format": 1, "writer": str(repo),
            "manifests": {name: digest(repo / name) for name in ("pyproject.toml", "uv.lock")},
            "python": interpreter(python), "uv_version": UV_VERSION, "uv_sha256": digest(uv)}


def encoded(value: dict[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def verify(repo: Path, generation: Path, expected: str) -> None:
    if generation != root(repo) / expected or generation.resolve(strict=True) != generation:
        raise ValueError("check generation is not writer-private and canonical")
    receipt = generation / "complete.json"
    data = receipt.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("check completion receipt changed")
    value = json.loads(data)
    current = specification(repo, generation / "bin/python", root(repo) / f"uv-{UV_VERSION}")
    if current != value:
        raise ValueError("check manifests, interpreter or bootstrap tool changed; rerun scripts/check")
    for tool in TOOLS:
        if not os.access(generation / "bin" / tool, os.X_OK):
            raise ValueError(f"check generation is incomplete: {tool}")


def prepare(repo: Path, uv: Path | None = None) -> tuple[str, str]:
    repo = repo.resolve(strict=True)
    directory = root(repo)
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("check directory must not be a symlink")
    # The lock includes tool installation and receipt publication; a dead creator
    # releases it automatically. Completed generations are never repaired in place.
    with (directory / "creator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        local_uv = directory / f"uv-{UV_VERSION}"
        if not local_uv.exists():
            if uv is None:
                common = subprocess.check_output(
                    ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                    text=True,
                ).strip()
                uv = Path(common) / "switchstand-tools" / f"uv-{UV_VERSION}"
            source = pinned_uv(uv)
            temporary = directory / "uv.pending"
            shutil.copyfile(source, temporary)
            temporary.chmod(0o700)
            pinned_uv(temporary)
            temporary.replace(local_uv)
        pinned_uv(local_uv)
        python = Path(sys.executable).resolve(strict=True)
        value = specification(repo, python, local_uv)
        data = encoded(value)
        key = hashlib.sha256(data).hexdigest()
        generation = directory / key
        if not (generation / "complete.json").exists():
            if generation.exists():
                shutil.rmtree(generation)
            environment = dict(os.environ)
            for name in tuple(environment):
                if name.startswith("UV_") or name in {"VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"}:
                    environment.pop(name)
            environment.update(UV_PROJECT_ENVIRONMENT=str(generation),
                               UV_CACHE_DIR=str(directory / "cache"),
                               UV_PYTHON_DOWNLOADS="never", UV_NO_CONFIG="1")
            subprocess.run(
                [str(local_uv), "sync", "--project", str(repo), "--locked", "--all-groups",
                 "--no-install-project", "--python", str(python)],
                env=environment, check=True,
            )
            if specification(repo, generation / "bin/python", local_uv) != value:
                raise ValueError("dependency inputs changed during environment creation")
            for tool in TOOLS:
                if not os.access(generation / "bin" / tool, os.X_OK):
                    raise ValueError(f"check generation is incomplete: {tool}")
            pending = generation / "receipt.pending"
            with pending.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            pending.replace(generation / "complete.json")
        verify(repo, generation, key)
        return str(generation), key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify"))
    parser.add_argument("repo", type=Path)
    parser.add_argument("--uv", type=Path)
    parser.add_argument("--generation", type=Path)
    parser.add_argument("--receipt")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            print("\n".join(prepare(args.repo, args.uv)))
        else:
            if args.generation is None or args.receipt is None:
                raise ValueError("generation and receipt are required")
            verify(args.repo.resolve(strict=True), args.generation, args.receipt)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"private check environment unavailable: {error}; "
                    "relaunch with CONTROL's pinned bootstrap tool\n")


if __name__ == "__main__":
    main()
