import json
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
[ -z "${FAKE_START_GATE:-}" ] || { while [ ! -f "$FAKE_START_GATE" ]; do sleep 0.01; done; }
printf '%s\n' "$*" >>"$FAKE_TRACE"
case "$*" in
  "compose "*" ps -q postgres") echo shared ;;
  "inspect --format "*" shared")
    case "$*" in
      *Mounts*) echo "$FAKE_MOUNT" ;;
      *"{{.Name}}"*) echo /shared-name ;;
      *) echo "$FAKE_IDENTITY" ;;
    esac ;;
  "network inspect --format "*) printf '%s\n' "$FAKE_MEMBERS" ;;
  "volume inspect --format "*) echo "$FAKE_VOLUME_IDENTITY" ;;
  "exec shared psql "*version_num*)
    [ ! -f "$FAKE_STATE/shared-revision" ] || { cat "$FAKE_STATE/shared-revision"; exit; }
    echo "$FAKE_REVISION" ;;
  "exec shared psql "*to_regclass*work_authority*) echo "$FAKE_STAGE1_EXISTS" ;;
  "exec shared psql "*to_regclass*work_metadata_authority*) echo "$FAKE_STAGE2_EXISTS" ;;
  "exec shared psql "*"count(*) FROM work_authority"*) echo "$FAKE_STAGE1_ROWS" ;;
  "exec shared psql "*"count(*) FROM work_metadata_authority"*) echo "$FAKE_STAGE2_ROWS" ;;
  "exec shared psql "*pg_stat_activity*) echo "$FAKE_CLIENTS" ;;
  "exec shared psql "*) echo "$FAKE_COUNTS" ;;
  "exec shared pg_dump "*--schema-only*) printf 'schema-%s%s' "$(cat "$FAKE_STATE/shared-revision" 2>/dev/null || echo "$FAKE_REVISION")" "$FAKE_DUMP_SUFFIX" ;;
  "exec shared pg_dump "*--data-only*) printf 'data-%s%s' "$(cat "$FAKE_STATE/shared-revision" 2>/dev/null || echo "$FAKE_REVISION")" "$FAKE_DUMP_SUFFIX" ;;
  "exec shared pg_dump "*) printf DUMP ;;
  "run -d "*) : ;;
  "exec switchstand-upgrade-rehearsal-"*" pg_isready "*) : ;;
  "cp "*) : ;;
  "exec switchstand-upgrade-rehearsal-"*" pg_restore "*) : ;;
  "exec switchstand-upgrade-rehearsal-"*" psql "*version_num*)
    cat "$FAKE_STATE/rehearsal-revision" 2>/dev/null || echo "$FAKE_REVISION" ;;
  "exec switchstand-upgrade-rehearsal-"*" psql "*) echo "$FAKE_COUNTS" ;;
  "exec switchstand-upgrade-rehearsal-"*" pg_dump "*--schema-only*) printf 'schema-%s' "$(cat "$FAKE_STATE/rehearsal-revision" 2>/dev/null || echo "$FAKE_REVISION")" ;;
  "exec switchstand-upgrade-rehearsal-"*" pg_dump "*--data-only*) printf 'data-%s' "$(cat "$FAKE_STATE/rehearsal-revision" 2>/dev/null || echo "$FAKE_REVISION")" ;;
  "build "*) : ;;
  "run --rm switchstand-upgrade:"*" heads") echo "$FAKE_HEADS" ;;
  "run --rm --network container:switchstand-upgrade-rehearsal-"*)
    [ "$FAKE_FAIL_REHEARSAL" = 0 ] || exit 17
    case "$*" in *" downgrade "*) for arg do last=$arg; done; echo "$last" >"$FAKE_STATE/rehearsal-revision" ;; *) echo 0010_work_metadata_authority >"$FAKE_STATE/rehearsal-revision" ;; esac ;;
  "run --rm --network container:shared "*)
    case "$*" in
      *" downgrade "*) for arg do last=$arg; done; echo "$last" >"$FAKE_STATE/shared-revision" ;;
      *) echo "$FAKE_FAILED_REVISION" >"$FAKE_STATE/shared-revision"; [ "$FAKE_FAIL_SHARED" = 0 ] || exit 17 ;;
    esac ;;
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
        "FAKE_REVISION": "0007_agent_chat_identity",
        "FAKE_COUNTS": "78|2|3|4|5|6|7|8|9",
        "FAKE_IDENTITY": "switchstand|postgres|postgres:18-alpine|healthy",
        "FAKE_MOUNT": "switchstand_postgres-data|true",
        "FAKE_VOLUME_IDENTITY": "switchstand|postgres-data",
        "FAKE_CLIENTS": "0",
        "FAKE_MEMBERS": "shared-name",
        "FAKE_FAIL_REHEARSAL": "0",
        "FAKE_FAIL_SHARED": "0",
        "FAKE_STAGE1_EXISTS": "f",
        "FAKE_STAGE2_EXISTS": "f",
        "FAKE_STAGE1_ROWS": "0",
        "FAKE_STAGE2_ROWS": "0",
        "FAKE_HEADS": "0010_work_metadata_authority (head)",
        "FAKE_FAILED_REVISION": "0010_work_metadata_authority",
        "FAKE_DUMP_SUFFIX": "",
    }
    return repo, env


def _run(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return _run_target(repo, env, "production", *args)


def _run_target(
    repo: Path, env: dict[str, str], target: str, *args: str | Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [repo / "scripts" / SCRIPT.name, "--target", target, *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _disposable(env: dict[str, str], name: str) -> Path:
    env.pop("SWITCHSTAND_BACKUP_DIR", None)
    parent = Path(env["HOME"]) / ".local/state/switchstand/rehearsals"
    target = parent / name
    target.mkdir(mode=0o700, parents=True)
    parent.chmod(0o700)
    target.chmod(0o700)
    return target


def _target_env(env: dict[str, str], project: str) -> None:
    env.update(FAKE_IDENTITY=f"{project}|postgres|postgres:18-alpine|healthy",
               FAKE_MOUNT=f"{project}_postgres-data|true",
               FAKE_VOLUME_IDENTITY=f"{project}|postgres-data")


def test_requires_explicit_target_before_docker(tmp_path):
    repo, env = _repo(tmp_path)
    result = subprocess.run(
        [repo / "scripts" / SCRIPT.name], env=env, text=True, capture_output=True, check=False
    )
    assert result.returncode == 2
    assert not Path(env["FAKE_TRACE"]).exists()


def test_disposable_target_derives_only_namespaced_docker_identities(tmp_path):
    repo, env = _repo(tmp_path)
    target = _disposable(env, "proof")
    project = "switchstand-rehearsal-proof"
    _target_env(env, project)
    result = _run_target(repo, env, "disposable:proof", "rehearse")
    assert result.returncode == 0, result.stderr
    trace = Path(env["FAKE_TRACE"]).read_text()
    assert f"-p {project}" in trace and f"{project}_default" in trace
    assert "-p switchstand --project" not in trace
    assert not (Path(env["HOME"]) / ".local/state/switchstand/backups").exists()
    assert len(list((target / "backups").glob("*.dump"))) == 1


def test_disposable_target_rejects_production_receipt_path_before_docker(tmp_path):
    repo, env = _repo(tmp_path)
    _disposable(env, "proof")
    receipt = Path(env["HOME"]) / ".local/state/switchstand/production.json"
    result = _run_target(repo, env, "disposable:proof", "apply", receipt)
    assert result.returncode != 0
    assert "escapes its private state directory" in result.stderr
    assert not Path(env["FAKE_TRACE"]).exists()


def test_disposable_target_rejects_symlink_to_production_before_artifacts(tmp_path):
    repo, env = _repo(tmp_path)
    env.pop("SWITCHSTAND_BACKUP_DIR")
    parent = Path(env["HOME"]) / ".local/state/switchstand/rehearsals"
    production = Path(env["HOME"]) / ".local/state/switchstand/production"
    parent.mkdir(mode=0o700, parents=True)
    parent.chmod(0o700)
    production.mkdir(mode=0o700)
    (parent / "proof").symlink_to(production, target_is_directory=True)

    result = _run_target(repo, env, "disposable:proof", "rehearse")
    assert result.returncode != 0
    assert "not canonical, owned, and private" in result.stderr
    assert not Path(env["FAKE_TRACE"]).exists()
    assert not (production / "state-upgrade.lock").exists()


def test_disposable_target_rejects_backup_override_before_artifacts(tmp_path):
    repo, env = _repo(tmp_path)
    target = _disposable(env, "proof")
    env["SWITCHSTAND_BACKUP_DIR"] = str(tmp_path / "production-backups")
    result = _run_target(repo, env, "disposable:proof", "rehearse")
    assert result.returncode != 0 and not Path(env["FAKE_TRACE"]).exists()
    assert not (target / "state-upgrade.lock").exists()


def test_disposable_backup_and_digest_temp_resist_symlinks(tmp_path):
    repo, env = _repo(tmp_path)
    target = _disposable(env, "proof")
    victim_dir = tmp_path / "production-backups"
    victim_dir.mkdir()
    (target / "backups").symlink_to(victim_dir, target_is_directory=True)
    result = _run_target(repo, env, "disposable:proof", "rehearse")
    assert result.returncode != 0 and not Path(env["FAKE_TRACE"]).exists()
    assert not list(victim_dir.iterdir())
    (target / "backups").unlink()
    (target / "backups").mkdir(mode=0o700)
    gate, victim = tmp_path / "docker-go", tmp_path / "production-digest"
    victim.write_text("unchanged")
    env["FAKE_START_GATE"] = str(gate)
    _target_env(env, "switchstand-rehearsal-proof")
    process = subprocess.Popen(
        [repo / "scripts" / SCRIPT.name, "--target", "disposable:proof", "rehearse"],
        env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    (target / f"state-upgrade-schema-{process.pid}.sql").symlink_to(victim)
    gate.touch()
    _, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr
    assert victim.read_text() == "unchanged"


def test_production_rejects_disposable_resume_and_abort_receipts_before_docker(tmp_path):
    repo, env = _repo(tmp_path)
    target = _disposable(env, "proof")
    project = "switchstand-rehearsal-proof"
    _target_env(env, project)
    receipt = target / "apply.json"
    applied = _run_target(repo, env, "disposable:proof", "apply", receipt)
    assert applied.returncode == 0, applied.stderr
    original = receipt.read_text()
    identity = {"target": "disposable:proof", "project": project,
                "volume": f"{project}_postgres-data", "network": f"{project}_default"}
    assert all(identity.items() <= value.items()
               for value in map(json.loads, original.splitlines()))
    abort_receipt = target / "abort-source.json"
    abort_receipt.write_text(original)
    records = [json.loads(line) for line in original.splitlines()]
    records[-1]["outcome"] = "UNKNOWN"
    receipt.write_text("".join(json.dumps(value) + "\n" for value in records))
    _target_env(env, "switchstand")
    trace = Path(env["FAKE_TRACE"])
    trace.unlink()
    resumed = _run(repo, env, "apply", str(receipt))
    aborted = _run(repo, env, "abort-pre-authority", str(abort_receipt))
    assert resumed.returncode != 0 and "stale or terminal" in resumed.stderr
    assert aborted.returncode != 0 and "stale or mismatched" in aborted.stderr
    assert not trace.exists()


def test_refuses_wrong_revision_before_backup_or_migration(tmp_path):
    repo, env = _repo(tmp_path)
    env["FAKE_REVISION"] = "unexpected"

    result = _run(repo, env)

    assert result.returncode == 1
    assert "expected exact source 0007_agent_chat_identity or 0010_work_metadata_authority; actual unexpected" in result.stderr
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


def test_failed_shared_migration_without_marker_downgrades_once(tmp_path):
    repo, env = _repo(tmp_path)
    env["FAKE_FAIL_SHARED"] = "1"
    receipt = tmp_path / "apply.json"

    result = _run(repo, env, "apply", str(receipt))

    assert result.returncode == 1
    assert "pre-authority downgrade completed and original state verified" in result.stderr
    trace = Path(env["FAKE_TRACE"]).read_text()
    assert trace.count("run --rm --network container:shared") == 2
    assert [json.loads(line)["outcome"] for line in receipt.read_text().splitlines()] == ["ATTEMPTING", "ABORTED"]


def test_failed_shared_migration_with_ambiguous_revision_never_downgrades(tmp_path):
    repo, env = _repo(tmp_path)
    env["FAKE_FAIL_SHARED"] = "1"
    env["FAKE_FAILED_REVISION"] = "0010_partial_unknown"

    receipt = tmp_path / "apply.json"
    result = _run(repo, env, "apply", str(receipt))

    assert result.returncode == 1
    assert "ambiguous Alembic revision 0010_partial_unknown; outcome UNKNOWN" in result.stderr
    assert Path(env["FAKE_TRACE"]).read_text().count("run --rm --network container:shared") == 1
    assert json.loads(receipt.read_text().splitlines()[-1])["outcome"] == "UNKNOWN"


def test_rehearses_before_shared_upgrade_and_preserves_backup(tmp_path):
    repo, env = _repo(tmp_path)

    result = _run(repo, env)

    assert result.returncode == 0, result.stderr
    assert "0007_agent_chat_identity -> 0010_work_metadata_authority" in result.stdout
    assert "preserved counts 78|2|3|4|5|6|7|8|9" in result.stdout
    backups = list((tmp_path / "backups").glob("*.dump"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"DUMP"
    assert backups[0].stat().st_mode & 0o777 == 0o600
    trace = Path(env["FAKE_TRACE"]).read_text()
    rehearsal = trace.index("run --rm --network container:switchstand-upgrade-rehearsal-")
    shared = trace.index("run --rm --network container:shared")
    assert rehearsal < shared
    assert (tmp_path / "state" / "rehearsal-revision").read_text().strip() == "0010_work_metadata_authority"
    assert (tmp_path / "state" / "shared-revision").read_text().strip() == "0010_work_metadata_authority"
    assert "--restrict-key=SWITCHSTANDSTATEDIGEST" in trace


def test_upgrades_existing_0004_and_preserves_all_existing_counts(tmp_path):
    repo, env = _repo(tmp_path)
    env["FAKE_REVISION"] = "0004_required_result_persistence"
    env["FAKE_COUNTS"] = "78|2|3|4|5|6|7"

    result = _run(repo, env)

    assert result.returncode == 1
    assert "expected exact source 0007_agent_chat_identity" in result.stderr


def test_upgrades_existing_0005_and_preserves_all_existing_counts(tmp_path):
    repo, env = _repo(tmp_path)
    env["FAKE_REVISION"] = "0005_work_event_handles"
    env["FAKE_COUNTS"] = "78|2|3|4|5|6|7|8"

    result = _run(repo, env)

    assert result.returncode == 1
    assert "expected exact source 0007_agent_chat_identity" in result.stderr
    trace = Path(env["FAKE_TRACE"]).read_text()
    assert "pg_dump" not in trace


def test_upgrades_existing_0006_and_preserves_agent_mailboxes(tmp_path):
    repo, env = _repo(tmp_path)
    env["FAKE_REVISION"] = "0006_agent_mailboxes"
    env["FAKE_COUNTS"] = "78|2|3|4|5|6|7|8|9"

    result = _run(repo, env)

    assert result.returncode == 1
    assert "expected exact source 0007_agent_chat_identity" in result.stderr
    trace = Path(env["FAKE_TRACE"]).read_text()
    assert "pg_dump" not in trace


def test_current_head_without_exact_receipt_is_unknown(tmp_path):
    repo, env = _repo(tmp_path)
    env["FAKE_REVISION"] = "0010_work_metadata_authority"

    result = _run(repo, env)

    assert result.returncode == 1
    assert "without an exact reconcilable receipt; outcome UNKNOWN" in result.stderr
    trace = Path(env["FAKE_TRACE"]).read_text()
    assert "pg_dump" not in trace
    assert "build" not in trace


def test_rehearse_cycles_without_mutating_shared_and_writes_private_receipt(tmp_path):
    repo, env = _repo(tmp_path)
    receipt = tmp_path / "rehearsal.json"

    result = _run(repo, env, "rehearse", str(receipt))

    assert result.returncode == 0, result.stderr
    assert "0007_agent_chat_identity -> 0010_work_metadata_authority -> 0007_agent_chat_identity -> 0010_work_metadata_authority" in result.stdout
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert '"outcome": "REHEARSED"' in receipt.read_text()
    assert "run --rm --network container:shared" not in Path(env["FAKE_TRACE"]).read_text()


def test_abort_uses_exact_apply_receipt_and_refuses_after_authority(tmp_path):
    repo, env = _repo(tmp_path)
    apply_receipt = tmp_path / "apply.json"
    assert _run(repo, env, "apply", str(apply_receipt)).returncode == 0
    env["FAKE_STAGE1_EXISTS"] = "t"
    env["FAKE_STAGE1_ROWS"] = "1"

    result = _run(repo, env, "abort-pre-authority", str(apply_receipt))

    assert result.returncode == 1
    assert "POSTGRES_AUTHORITY present; downgrade forbidden" in result.stderr
    assert Path(env["FAKE_STATE"]).joinpath("shared-revision").read_text().strip() == "0010_work_metadata_authority"


def test_abort_before_authority_restores_exact_original_digest(tmp_path):
    repo, env = _repo(tmp_path)
    apply_receipt = tmp_path / "apply.json"
    assert _run(repo, env, "apply", str(apply_receipt)).returncode == 0

    result = _run(repo, env, "abort-pre-authority", str(apply_receipt))

    assert result.returncode == 0, result.stderr
    assert "ABORTED_PRE_AUTHORITY" in result.stdout
    assert Path(env["FAKE_STATE"]).joinpath("shared-revision").read_text().strip() == "0007_agent_chat_identity"


def test_abort_refuses_when_applied_database_no_longer_matches_receipt(tmp_path):
    repo, env = _repo(tmp_path)
    receipt = tmp_path / "apply.json"
    assert _run(repo, env, "apply", str(receipt)).returncode == 0
    env["FAKE_DUMP_SUFFIX"] = "-corrupt"

    result = _run(repo, env, "abort-pre-authority", str(receipt))

    assert result.returncode == 1
    assert "no longer matches exact applied receipt" in result.stderr


def test_apply_reconciles_fsynced_attempt_after_effect_without_retry(tmp_path):
    repo, env = _repo(tmp_path)
    receipt = tmp_path / "apply.json"
    assert _run(repo, env, "apply", str(receipt)).returncode == 0
    receipt.write_text(receipt.read_text().splitlines()[0] + "\n")
    before = Path(env["FAKE_TRACE"]).read_text().count("run --rm --network container:shared")

    result = _run(repo, env, "apply", str(receipt))

    assert result.returncode == 0, result.stderr
    assert "APPLIED reconciled" in result.stdout
    assert Path(env["FAKE_TRACE"]).read_text().count("run --rm --network container:shared") == before


def test_unknown_observation_cannot_replace_intended_after_identity(tmp_path):
    repo, env = _repo(tmp_path)
    receipt = tmp_path / "apply.json"
    assert _run(repo, env, "apply", str(receipt)).returncode == 0
    attempt = json.loads(receipt.read_text().splitlines()[0])
    observed = {**attempt, "outcome": "UNKNOWN", "after_data": "0" * 64}
    receipt.write_text(json.dumps(attempt) + "\n" + json.dumps(observed) + "\n")
    env["FAKE_DUMP_SUFFIX"] = "-corrupt"

    result = _run(repo, env, "apply", str(receipt))

    assert result.returncode == 1
    assert json.loads(receipt.read_text().splitlines()[-1])["outcome"] == "UNKNOWN"


def test_candidate_with_unexpected_alembic_head_is_rejected(tmp_path):
    repo, env = _repo(tmp_path)
    env["FAKE_HEADS"] = "0011_surprise (head)"

    result = _run(repo, env, "rehearse", str(tmp_path / "receipt.json"))

    assert result.returncode == 1
    assert "candidate migration head mismatch" in result.stderr
