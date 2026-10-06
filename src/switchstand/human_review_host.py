"""Render an inert user-systemd asset for the loopback Human Review runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

SERVICE_NAME = "switchstand-human-review.service"
SYSTEMD_LITERAL_PATH = re.compile(r"/[A-Za-z0-9._/@:+-]+")


@dataclass(frozen=True, slots=True)
class HumanReviewHostAssets:
    runtime_python: Path
    runtime_root: Path
    environment_file: Path
    bind_port: int

    def __post_init__(self) -> None:
        for value in (self.runtime_python, self.runtime_root, self.environment_file):
            if not value.is_absolute():
                raise ValueError("Human Review host paths must be absolute")
            if SYSTEMD_LITERAL_PATH.fullmatch(str(value)) is None:
                raise ValueError("Human Review host paths contain unsupported systemd characters")
        if not 1 <= self.bind_port <= 65535:
            raise ValueError("Human Review bind port must be between 1 and 65535")


def systemd_unit(assets: HumanReviewHostAssets) -> str:
    return (
        "[Unit]\nAfter=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"EnvironmentFile={assets.environment_file}\n"
        f"Environment=PYTHONPATH={assets.runtime_root}/src\n"
        "Environment=SWITCHSTAND_HUMAN_REVIEW_BIND_HOST=127.0.0.1\n"
        f"Environment=SWITCHSTAND_HUMAN_REVIEW_BIND_PORT={assets.bind_port}\n"
        f"ExecStart={assets.runtime_python} -m switchstand.human_review_runtime\n"
        "Restart=on-failure\nRestartSec=2\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def render_service(assets: HumanReviewHostAssets, output: Path) -> dict[str, str]:
    if not output.is_absolute() or not output.is_dir() or output.is_symlink():
        raise ValueError("output must be an existing absolute real directory")
    if (
        not assets.runtime_root.is_dir()
        or assets.runtime_root.is_symlink()
        or not (assets.runtime_root / "src/switchstand").is_dir()
    ):
        raise ValueError("runtime root must contain the Switchstand source tree")
    try:
        python_metadata = assets.runtime_python.lstat()
        environment_metadata = assets.environment_file.lstat()
    except OSError as error:
        raise ValueError("Human Review host input is unavailable") from error
    if (
        assets.runtime_python.is_symlink()
        or not stat.S_ISREG(python_metadata.st_mode)
        or not os.access(assets.runtime_python, os.X_OK)
    ):
        raise ValueError("runtime Python must be a real executable file")
    if (
        assets.environment_file.is_symlink()
        or not stat.S_ISREG(environment_metadata.st_mode)
        or environment_metadata.st_uid != os.getuid()
        or stat.S_IMODE(environment_metadata.st_mode) != 0o600
    ):
        raise ValueError("environment file must be an owned mode-0600 regular file")

    content = systemd_unit(assets)
    target = output / SERVICE_NAME
    staging = output / f".{SERVICE_NAME}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(
            staging,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o644,
        )
    except FileExistsError as error:  # pragma: no cover - unpredictable token collision
        raise RuntimeError("Human Review staging asset collision") from error
    encoded = content.encode()
    try:
        try:
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written == 0:
                    raise OSError("short write while rendering Human Review service")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(staging, target, follow_symlinks=False)
        except FileExistsError as error:
            raise ValueError(f"refusing to replace rendered asset: {SERVICE_NAME}") from error
    finally:
        if staging.exists():
            staging.unlink()
    return {SERVICE_NAME: hashlib.sha256(encoded).hexdigest()}


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-python", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--environment-file", required=True, type=Path)
    parser.add_argument("--bind-port", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    assets = HumanReviewHostAssets(
        arguments.runtime_python,
        arguments.runtime_root,
        arguments.environment_file,
        arguments.bind_port,
    )
    print(json.dumps(render_service(assets, arguments.output), sort_keys=True))


if __name__ == "__main__":
    run()
