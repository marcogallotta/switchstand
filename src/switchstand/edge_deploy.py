"""One-command production deployment for the single-user ChatGPT edge."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO, cast

from alembic.script import ScriptDirectory

from .edge_semantic_probe import MCP, ProbeFailure, _private, _runtime
from .stable_auth_deployment import _env

SERVICE = "switchstand-chatgpt-mcp.service"
LAUNCHER = Path.home() / ".local/libexec/switchstand-chatgpt-edge"
ENV_FILE = Path.home() / ".config/switchstand/mcp.env"
STATE_ROOT = Path.home() / ".local/state/switchstand"
SMOKE_BIND_SECONDS = 10
LOCAL_URL = "http://127.0.0.1:8790/mcp"
CONTAINER = "switchstand-postgres-1"
CURRENTNESS_START = "    # switchstand-deploy currentness start\n"
CURRENTNESS_END = "    # switchstand-deploy currentness end\n"


def command(
    arguments: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments, check=True, capture_output=True, text=True, env=env, timeout=timeout
    )


def git(path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return command(["git", "-C", str(path), *arguments])


def db_command(tool: str, *arguments: str, interactive: bool = False) -> list[str]:
    prefix = ["docker", "exec", *(["-i"] if interactive else []), CONTAINER]
    return [*prefix, tool, *arguments, "-U", "switchstand"]


def _runtime_literal(source: str) -> tuple[Path, int, int]:
    matches: list[ast.Constant] = []
    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "RUNTIME" for target in node.targets
            )
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "Path"
            and len(node.value.args) == 1
            and isinstance(node.value.args[0], ast.Constant)
            and isinstance(node.value.args[0].value, str)
        ):
            matches.append(node.value.args[0])
    if len(matches) != 1 or matches[0].end_lineno is None or matches[0].end_col_offset is None:
        raise RuntimeError("launcher has no single exact RUNTIME literal")
    value = matches[0]
    assert value.end_lineno is not None and value.end_col_offset is not None
    lines = source.splitlines(keepends=True)
    start = sum(map(len, lines[: value.lineno - 1])) + value.col_offset
    end = sum(map(len, lines[: value.end_lineno - 1])) + value.end_col_offset
    return Path(cast(str, value.value)), start, end


def candidate_launcher(current: str, runtime: Path, candidate_sha: str) -> tuple[Path, str]:
    old_runtime, start, end = _runtime_literal(current)
    if '"SWITCHSTAND_OUTCOME_STATE_ACTIONS": "1"' not in current:
        raise RuntimeError("production launcher lacks the outcome-state flag")
    rendered = current[:start] + repr(str(runtime)) + current[end:]
    block = (
        CURRENTNESS_START
        + "    os.environ.update(\n"
        + "        {\n"
        + f'            "SWITCHSTAND_PRODUCT_CURRENTNESS_RUNTIME_SHA": "{candidate_sha}",\n'
        + f'            "SWITCHSTAND_PRODUCT_CURRENTNESS_SELECTED_RUNTIME_SHA": "{candidate_sha}",\n'
        + f'            "SWITCHSTAND_PRODUCT_CURRENTNESS_RUN_ID": "edge-deploy-{candidate_sha[:12]}",\n'
        + "        }\n"
        + "    )\n"
        + CURRENTNESS_END
    )
    if CURRENTNESS_START in rendered or CURRENTNESS_END in rendered:
        if rendered.count(CURRENTNESS_START) != 1 or rendered.count(CURRENTNESS_END) != 1:
            raise RuntimeError("production launcher has malformed currentness block")
        prefix, remainder = rendered.split(CURRENTNESS_START, 1)
        _old, suffix = remainder.split(CURRENTNESS_END, 1)
        rendered = prefix + block + suffix
    else:
        anchor = "    os.environ.update(oauth)\n"
        if rendered.count(anchor) != 1:
            raise RuntimeError("production launcher lacks the exact OAuth environment anchor")
        rendered = rendered.replace(anchor, anchor + block)
    return old_runtime, rendered


def authenticated_smoke(expected_sha: str) -> None:
    values = _env(ENV_FILE)
    try:
        token_file = Path(values["SWITCHSTAND_R0_TOKEN_FILE"])
        work_id = values["SWITCHSTAND_R0_WORK_ID"]
    except KeyError as exc:
        raise RuntimeError("production smoke inputs are missing") from exc
    client = MCP(LOCAL_URL, _private(token_file))
    try:
        client.initialize()
        _runtime(client.client, LOCAL_URL, expected_sha)
        result = client.call(
            "tools/call",
            {
                "name": "work_get",
                "arguments": {"api_version": "1", "work_id": work_id},
            },
            2,
        )
        result_value = cast(dict[str, object], result) if isinstance(result, dict) else {}
        if result_value.get("status") != "ok":
            raise RuntimeError("logged-in work_get smoke did not return ok")
    except (KeyError, OSError, ProbeFailure, TypeError, ValueError) as exc:
        raise RuntimeError("logged-in MCP smoke failed") from exc
    finally:
        client.close()


class Deployer:
    def __init__(self, repo: Path, candidate_sha: str, log: TextIO):
        self.repo = repo
        self.candidate_sha = candidate_sha
        self.log = log
        self.attempt = STATE_ROOT / "deployments" / f"edge-{candidate_sha[:12]}"
        self.runtime = self.attempt / "runtime"
        self.launcher_before = self.attempt / "launcher.before"
        self.launcher_candidate = self.attempt / "launcher"
        self.code_record = self.attempt / "code-only.json"
        self.database_backup: Path | None = None
        self.cutover_started = False

    def service(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return command(["systemctl", "--user", *arguments, SERVICE])

    def record(self, step: str, started: float, result: str) -> None:
        line = f"step={step} seconds={time.monotonic() - started:.3f} result={result}"
        print(line)
        self.log.write(line + "\n")
        self.log.flush()

    def step(self, name: str, action: Callable[[], None]) -> None:
        started = time.monotonic()
        try:
            action()
        except BaseException:
            self.record(name, started, "FAIL")
            raise
        self.record(name, started, "PASS")

    def control_env(self) -> dict[str, str]:
        common = git(
            self.runtime, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).stdout.strip()
        return os.environ | {
            "SWITCHSTAND_CONTROL_PATH": str(self.runtime),
            "SWITCHSTAND_CONTROL_SHA": self.candidate_sha,
            "SWITCHSTAND_CONTROL_COMMON": common,
        }

    def prepare(self) -> None:
        if self.code_record.exists():
            raise RuntimeError("retained code-only attempt requires reconciliation")
        refspec = "+refs/heads/main:refs/remotes/origin/main"
        git(self.repo, "fetch", "--quiet", "--no-tags", "origin", refspec)
        accepted = git(self.repo, "rev-parse", "origin/main").stdout.strip()
        if accepted != self.candidate_sha:
            raise RuntimeError("candidate must be the exact current origin/main")
        self.attempt.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.runtime.exists():
            actual = git(self.runtime, "rev-parse", "HEAD").stdout.strip()
            dirty = git(self.runtime, "status", "--porcelain").stdout
            if actual != self.candidate_sha or dirty:
                raise RuntimeError("retained candidate runtime does not match")
        else:
            git(self.repo, "worktree", "add", "--detach", str(self.runtime), self.candidate_sha)
        current = LAUNCHER.read_text()
        old_runtime, rendered = candidate_launcher(current, self.runtime, self.candidate_sha)
        if git(old_runtime, "status", "--porcelain").stdout:
            raise RuntimeError("current runtime is dirty")
        shutil.copy2(LAUNCHER, self.launcher_before)
        self.launcher_before.chmod(0o600)
        self.launcher_candidate.write_text(rendered)
        self.launcher_candidate.chmod(0o700)
        command([str(self.launcher_candidate), "--verify-import-only"], env=self.control_env())

    def backup(self) -> None:
        tool = str(self.runtime / "scripts/switchstand-backup")
        output = command(
            [tool, "run", "--container", CONTAINER, "--runtime-wrapper", str(LAUNCHER)],
            timeout=900,
        ).stdout.strip()
        backup = Path(output)
        if not backup.is_absolute() or not backup.is_file() or backup.stat().st_size == 0:
            raise RuntimeError("database backup result is invalid")
        self.database_backup = backup

    def migrate(self) -> None:
        upgrader = str(self.runtime / "scripts/switchstand-upgrade-state")
        image = command(
            [upgrader, "--mode", "prepare", "--target", "production"],
            env=self.control_env(),
            timeout=1800,
        ).stdout.strip()
        self.cutover_started = True
        self.stop()
        command(
            [upgrader, "--mode", "apply", "--target", "production", "--image-id", image],
            env=self.control_env(),
            timeout=1800,
        )

    def swap_restart(self) -> None:
        temporary = LAUNCHER.with_name(f".{LAUNCHER.name}.deploy.tmp")
        shutil.copy2(self.launcher_candidate, temporary)
        temporary.chmod(0o700)
        os.replace(temporary, LAUNCHER)
        self.service("restart")

    def stop(self) -> None:
        self.service("stop")
        pid = self.service("show", "--property=MainPID", "--value").stdout.strip()
        if pid != "0":
            raise RuntimeError(f"service did not quiesce: MainPID={pid or 'unknown'}")

    def local_smoke(self, runtime: Path, sha: str) -> None:
        selected, _, _ = _runtime_literal(LAUNCHER.read_text())
        if selected != runtime:
            raise RuntimeError("running service is not bound to the selected runtime")
        # Right after restart MainPID can still be the pre-exec child; wait briefly for exec.
        deadline = time.monotonic() + SMOKE_BIND_SECONDS
        while True:
            pid = self.service("show", "--property=MainPID", "--value").stdout.strip()
            try:
                command_line = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            except OSError:
                command_line = []
            if pid.isdigit() and pid != "0" and os.fsencode(LAUNCHER) in command_line:
                break
            if time.monotonic() >= deadline:
                if not pid.isdigit() or pid == "0":
                    raise RuntimeError("service has no running MainPID")
                raise RuntimeError("running service is not bound to the selected runtime")
            time.sleep(0.2)
        doctor = str(runtime / "scripts/switchstand-edge-doctor")
        command(
            [doctor, "--env-file", str(ENV_FILE), "--repo", str(runtime), "--expected-sha", sha]
        )

    def smoke(self, runtime: Path, sha: str) -> None:
        self.local_smoke(runtime, sha)
        authenticated_smoke(sha)

    def restore_database(self) -> None:
        if self.database_backup is None:
            return
        metadata = json.loads(self.database_backup.with_suffix(".json").read_text())

        command(db_command("dropdb", "--force", "switchstand"))
        command(db_command("createdb", "-O", "switchstand", "switchstand"))
        with self.database_backup.open("rb") as stream:
            restore = db_command(
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "-d",
                "switchstand",
                interactive=True,
            )
            subprocess.run(
                restore,
                stdin=stream,
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=600,
            )
        revision = command(
            db_command(
                "psql",
                "-X",
                "-Atc",
                "SELECT version_num FROM alembic_version",
                "-d",
                "switchstand",
            )
        )
        probe = command(
            [
                str(self.runtime / "scripts/switchstand-backup"),
                "fingerprints",
                "--container",
                CONTAINER,
            ]
        ).stdout
        if revision.stdout.strip() != metadata.get("db_migration_version") or json.loads(
            probe
        ) != metadata.get("data_fingerprints"):
            raise RuntimeError("restored database does not match its backup")

    def rollback(self) -> None:
        if not self.cutover_started:
            return
        self.stop()
        self.restore_database()
        shutil.copy2(self.launcher_before, LAUNCHER)
        LAUNCHER.chmod(0o700)
        self.service("restart")
        old_runtime, _, _ = _runtime_literal(self.launcher_before.read_text())
        old_sha = git(old_runtime, "rev-parse", "HEAD").stdout.strip()
        self.local_smoke(old_runtime, old_sha)

    def code_identity(self) -> str:
        before = self.launcher_before.read_text()
        old_runtime, _, _ = _runtime_literal(before)
        sources: list[str] = []
        for runtime in (old_runtime, self.runtime):
            if git(runtime, "status", "--porcelain").stdout:
                raise RuntimeError("code-only runtime is dirty")
            sources.append(git(runtime, "rev-parse", "HEAD", "HEAD^{tree}").stdout)
        if sources[1].splitlines()[0] != self.candidate_sha:
            raise RuntimeError("code-only candidate identity changed")
        protected = ("migrations", "alembic.ini", "pyproject.toml", "uv.lock")
        manifests = [
            git(path, "ls-tree", "-r", "HEAD", "--", *protected).stdout
            for path in (old_runtime, self.runtime)
        ]
        schema = command(
            db_command(
                "psql", "-X", "-Atc", "SELECT version_num FROM alembic_version", "-d", "switchstand"
            )
        ).stdout.strip()
        if manifests[0] != manifests[1] or ScriptDirectory(
            str(old_runtime / "migrations")
        ).get_heads() != [schema]:
            raise RuntimeError("code-only dependencies or schema differ")
        digests = [
            hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.launcher_before, self.launcher_candidate, ENV_FILE)
        ]
        return json.dumps([self.candidate_sha, sources, manifests[0], schema, digests])

    def rollback_code_only(self) -> None:
        if (
            LAUNCHER.read_bytes() != self.launcher_candidate.read_bytes()
            or self.code_identity() != self.code_record.read_text()
        ):
            raise RuntimeError("code-only rollback identity changed or unavailable")
        self.local_smoke(self.runtime, self.candidate_sha)
        self.cutover_started = True
        self.rollback()
        old_runtime, _, _ = _runtime_literal(self.launcher_before.read_text())
        authenticated_smoke(git(old_runtime, "rev-parse", "HEAD").stdout.strip())

    def run(self, code_only_from: str | None = None) -> None:
        try:
            if code_only_from:
                old_runtime, _, _ = _runtime_literal(LAUNCHER.read_text())
                if (
                    git(old_runtime, "rev-parse", "HEAD").stdout.strip() != code_only_from
                    or code_only_from == self.candidate_sha
                    or (
                        self.launcher_before.exists()
                        and self.launcher_before.read_bytes() != LAUNCHER.read_bytes()
                    )
                ):
                    raise RuntimeError("code-only preimage or attempt does not match")
            self.step("prepare_candidate", self.prepare)
            if code_only_from:
                identity = self.code_identity()
                with self.code_record.open("x") as record:
                    record.write(identity)
                self.cutover_started = True
            else:
                self.step("backup_database", self.backup)
                self.step("migrate", self.migrate)
            self.step("swap_restart", self.swap_restart)
            self.step("smoke", lambda: self.smoke(self.runtime, self.candidate_sha))
        except BaseException:
            self.step("rollback", self.rollback)
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate_sha")
    parser.add_argument("--repo", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--code-only-from", help="reviewed/replay-qualified exact old SHA")
    mode.add_argument("--rollback-code-only", action="store_true")
    args = parser.parse_args(argv)
    if len(args.candidate_sha) != 40 or any(
        c not in "0123456789abcdef" for c in args.candidate_sha
    ):
        parser.error("candidate_sha must be a lowercase 40-character Git SHA")
    STATE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = STATE_ROOT / "edge-maintenance.lock"
    log_path = STATE_ROOT / "edge-deploy.log"
    with lock_path.open("a") as lock, log_path.open("a") as log:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("another edge deployment holds the lock")
        try:
            deployer = Deployer(args.repo.resolve(), args.candidate_sha, log)
            if args.rollback_code_only:
                deployer.step("rollback_code_only", deployer.rollback_code_only)
            else:
                deployer.run(args.code_only_from)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            print(f"DEPLOY FAIL {type(exc).__name__}: {exc}")
            return 1
    print(f"DEPLOY PASS sha={args.candidate_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
