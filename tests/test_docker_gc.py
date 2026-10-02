from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "docker-gc"


def test_gc_bounds_build_cache_without_touching_runtime_data(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >>"$DOCKER_LOG"\n'
        "echo 'Total reclaimed space: 0B'\n"
    )
    docker.chmod(0o755)
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DOCKER_LOG": str(log),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    result = subprocess.run(
        [SCRIPT], env=env, text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "DOCKER_GC_RESULT=PASS KEEP_STORAGE=20GB\n"
    assert log.read_text().splitlines() == [
        "builder prune --all --force --keep-storage 20GB"
    ]


def test_gc_failure_is_visible(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\necho 'daemon unavailable' >&2\nexit 17\n")
    docker.chmod(0o755)
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    result = subprocess.run(
        [SCRIPT], env=env, text=True, capture_output=True, check=False,
    )

    assert result.returncode == 1
    assert "daemon unavailable" in result.stderr
    assert "Docker cache GC failed" in result.stderr
