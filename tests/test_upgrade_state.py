import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "switchstand-upgrade-state"
HEAD = "a" * 40


def _repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    bin_dir = tmp_path / "bin"
    repo.mkdir()
    bin_dir.mkdir()
    (repo / "scripts").mkdir()
    launcher = repo / "scripts" / SCRIPT.name
    launcher.write_bytes(SCRIPT.read_bytes())
    launcher.chmod(0o755)
    (repo / "compose.state.yaml").write_text("services: {}\n")
    common = tmp_path / "common"
    common.mkdir()

    git = bin_dir / "git"
    git.write_text(
        f"""#!/bin/sh
case "$*" in
  *"rev-parse HEAD") echo {HEAD} ;;
  *"branch --show-current") : ;;
  *"rev-parse --path-format=absolute --git-common-dir") echo {common} ;;
  *"status --porcelain --untracked-files=all") : ;;
  *) echo "unexpected git call: $*" >&2; exit 90 ;;
esac
"""
    )
    git.chmod(0o755)

    docker = bin_dir / "docker"
    docker.write_text(
        """#!/bin/sh
set -eu
printf '%s\n' "$*" >>"$FAKE_TRACE"
case "$*" in
  "compose "*" ps -q postgres") echo shared ;;
  "inspect --format "*" shared")
    case "$*" in
      *Mounts*) echo "$FAKE_MOUNT" ;;
      *"{{.Name}}"*) echo /shared-name ;;
      *) echo "$FAKE_IDENTITY" ;;
    esac ;;
  "network inspect --format "*" switchstand_default") printf '%s\n' "$FAKE_MEMBERS" ;;
  "volume inspect --format "*" switchstand_postgres-data")
    echo "switchstand|postgres-data" ;;
  "exec shared psql "*version_num*)
    if [ -f "$FAKE_STATE/shared-new" ]; then
      echo 0005_work_event_handles
    else
      echo "$FAKE_REVISION"
    fi ;;
  "exec shared psql "*pg_stat_activity*) echo "$FAKE_CLIENTS" ;;
  "exec shared psql "*) echo "$FAKE_COUNTS" ;;
  "exec shared pg_dump "*) printf DUMP ;;
  "run -d "*) : ;;
  "exec switchstand-upgrade-rehearsal-"*" pg_isready "*) : ;;
  "cp "*) : ;;
  "exec switchstand-upgrade-rehearsal-"*" pg_restore "*) : ;;
  "exec switchstand-upgrade-rehearsal-"*" psql "*version_num*)
    if [ -f "$FAKE_STATE/rehearsal-new" ]; then
      echo 0005_work_event_handles
    else
      echo "$FAKE_REVISION"
    fi ;;
  "exec switchstand-upgrade-rehearsal-"*" psql "*) echo "$FAKE_COUNTS" ;;
  "build "*) : ;;
  "run --rm --network container:switchstand-upgrade-rehearsal-"*)
    [ "$FAKE_FAIL_REHEARSAL" = 0 ] || exit 17
    : >"$FAKE_STATE/rehearsal-new" ;;
  "run --rm --network container:shared "*)
    : >"$FAKE_STATE/shared-new"
    [ "$FAKE_FAIL_SHARED" = 0 ] || exit 17 ;;
  "rm -f "*) : ;;
  *) echo "unexpected docker call: $*" >&2; exit 91 ;;
esac
"""
    )
    docker.chmod(0o755)

    trace = tmp_path / "trace"
    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "SWITCHSTAND_CONTROL_PATH": str(repo),
        "SWITCHSTAND_CONTROL_SHA": HEAD,
        "SWITCHSTAND_CONTROL_COMMON": str(common),
        "SWITCHSTAND_BACKUP_DIR": str(tmp_path / "backups"),
        "FAKE_TRACE": str(trace),
        "FAKE_STATE": str(state),
        "FAKE_REVISION": "0002_grants_and_effects",
        "FAKE_COUNTS": "78|2|3",
        "FAKE_IDENTITY": "switchstand|postgres|postgres:18-alpine|healthy",
        "FAKE_MOUNT": "switchstand_postgres-data|true",
        "FAKE_CLIENTS": "0",
        "FAKE_MEMBERS": "shared-name",
        "FAKE_FAIL_REHEARSAL": "0",
        "FAKE_FAIL_SHARED": "0",
    }
    return repo, env


def _run(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [repo / "scripts" / SCRIPT.name],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_refuses_wrong_revision_before_backup_or_migration(tmp_path):
    repo, env = _repo(tmp_path)
    env["FAKE_REVISION"] = "unexpected"

    result = _run(repo, env)

    assert result.returncode == 1
    assert "expected one of 0002_grants_and_effects, 0004_required_result_persistence, 0005_work_event_handles; actual unexpected" in result.stderr
    trace = Path(env["FAKE_TRACE"]).read_text()
    assert "pg_dump" not in trace
    assert "build" not in trace


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("FAKE_IDENTITY", "foreign|postgres|postgres:18-alpine|healthy", "service identity"),
        ("FAKE_MOUNT", "foreign-volume|true", "volume identity"),
    ],
)
def test_refuses_wrong_service_or_volume_before_backup(
    tmp_path, variable, value, message
):
    repo, env = _repo(tmp_path)
    env[variable] = value

    result = _run(repo, env)

    assert result.returncode == 1
    assert message in result.stderr
    assert "pg_dump" not in Path(env["FAKE_TRACE"]).read_text()


def test_concurrent_invocation_refuses_before_docker(tmp_path):
    repo, env = _repo(tmp_path)
    lock = Path(env["HOME"]) / ".local/state/switchstand/state-upgrade.lock"
    lock.mkdir(parents=True)

    result = _run(repo, env)

    assert result.returncode == 1
    assert "another Switchstand state upgrade is active" in result.stderr
    assert not Path(env["FAKE_TRACE"]).exists()


def test_rehearsal_failure_never_migrates_shared_state(tmp_path):
    repo, env = _repo(tmp_path)
    env["FAKE_FAIL_REHEARSAL"] = "1"

    result = _run(repo, env)

    assert result.returncode == 17
    assert "shared migration NOT_RUN; operation aborted" in result.stderr
    trace = Path(env["FAKE_TRACE"]).read_text()
    assert "run --rm --network container:shared" not in trace
    assert list((tmp_path / "backups").glob("*.dump"))


def test_attached_writer_container_refuses_before_backup(tmp_path):
    repo, env = _repo(tmp_path)
    env["FAKE_MEMBERS"] = "shared-name\nactive-controller"

    result = _run(repo, env)

    assert result.returncode == 1
    assert "attached application container" in result.stderr
    assert "pg_dump" not in Path(env["FAKE_TRACE"]).read_text()


def test_ambiguous_shared_failure_reports_readback_without_retry(tmp_path):
    repo, env = _repo(tmp_path)
    env["FAKE_FAIL_SHARED"] = "1"

    result = _run(repo, env)

    assert result.returncode == 17
    assert "shared migration outcome UNKNOWN" in result.stderr
    assert "observed revision 0005_work_event_handles" in result.stderr
    assert "no retry or rollback attempted" in result.stderr
    trace = Path(env["FAKE_TRACE"]).read_text()
    assert trace.count("run --rm --network container:shared") == 1


def test_rehearses_before_shared_upgrade_and_preserves_backup(tmp_path):
    repo, env = _repo(tmp_path)

    result = _run(repo, env)

    assert result.returncode == 0, result.stderr
    assert "0002_grants_and_effects -> 0005_work_event_handles" in result.stdout
    assert "preserved counts 78|2|3" in result.stdout
    backups = list((tmp_path / "backups").glob("*.dump"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"DUMP"
    assert backups[0].stat().st_mode & 0o777 == 0o600
    trace = Path(env["FAKE_TRACE"]).read_text()
    rehearsal = trace.index("run --rm --network container:switchstand-upgrade-rehearsal-")
    shared = trace.index("run --rm --network container:shared")
    assert rehearsal < shared
    assert (tmp_path / "state" / "rehearsal-new").exists()
    assert (tmp_path / "state" / "shared-new").exists()


def test_upgrades_existing_0004_and_preserves_all_existing_counts(tmp_path):
    repo, env = _repo(tmp_path)
    env["FAKE_REVISION"] = "0004_required_result_persistence"
    env["FAKE_COUNTS"] = "78|2|3|4|5|6|7"

    result = _run(repo, env)

    assert result.returncode == 0, result.stderr
    assert "0004_required_result_persistence -> 0005_work_event_handles" in result.stdout
    assert "preserved counts 78|2|3|4|5|6|7" in result.stdout


def test_current_0005_is_a_noop_without_backup_or_build(tmp_path):
    repo, env = _repo(tmp_path)
    env["FAKE_REVISION"] = "0005_work_event_handles"

    result = _run(repo, env)

    assert result.returncode == 0, result.stderr
    assert "already at 0005_work_event_handles" in result.stdout
    trace = Path(env["FAKE_TRACE"]).read_text()
    assert "pg_dump" not in trace
    assert "build" not in trace
