import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple
from uuid import UUID

from .codex_runtime import codex_command, readback, validate_codex_args
from .development import (
    DevelopmentBoundary,
    cleanup_development,
    development_names,
    prepare_development,
)
from .docker import inspect as inspect_docker
from .run import RunReceipt, reserve_run
from .task_ref import asana_task_id

AUTHORITY_NAMES = ("ACTIVE_WORK_ID", "REFERENCE_WORK_IDS")
MANAGED_NAME = "SWITCHSTAND_MANAGED"
REQUESTING_GIT_COMMON = "SWITCHSTAND_REQUESTING_GIT_COMMON"
CHILD_TERM_SECONDS = 1.0


class Authority(NamedTuple):
    active: UUID
    references: tuple[UUID, ...]


class PreparedRun(NamedTuple):
    authority: Authority
    development: DevelopmentBoundary
    receipt: RunReceipt


def linked_branch(repo: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir",
         "HEAD", "--abbrev-ref", "HEAD"],
        cwd=repo,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    git_value, common_value, head, branch = completed.stdout.splitlines()
    git_dir, common_dir = (Path(value).resolve(strict=True) for value in (git_value, common_value))
    if git_dir == common_dir:
        raise ValueError("managed launch requires a linked writer worktree")
    if branch == "HEAD":
        raise ValueError("managed launch requires a branch")
    green = (git_dir / "switchstand-green-sha").read_text().strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo, env=env, check=True,
        text=True, capture_output=True,
    ).stdout
    based_on_green = head == green or subprocess.run(
        ["git", "merge-base", "--is-ancestor", green, head],
        cwd=repo, env=env, check=False, capture_output=True,
    ).returncode == 0
    dirty_task_checkpoint = branch.startswith("v2-task-") and based_on_green
    if not based_on_green or (dirty and not dirty_task_checkpoint):
        raise ValueError(
            "managed launch requires a clean writer based on its green baseline "
            "or a task writer at its exact checkpoint"
        )
    return branch


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Bind human-readable work and start Codex inside the managed boundary."
    )
    result.add_argument("--active", required=True, help="active Asana task ID or URL")
    result.add_argument("--commit", required=True, help="exact candidate commit SHA")
    result.add_argument(
        "--reference", action="append", default=[], help="read-only Asana task ID or URL"
    )
    result.add_argument("codex_args", nargs=argparse.REMAINDER, help="arguments passed to Codex")
    return result


def exact_revision_preflight(
    control: Path,
    candidate: Path,
    requested: str,
    control_sha: str,
    expected_common: str,
    env: dict[str, str],
    *,
    allow_dirty_task: bool = False,
) -> str:
    if len(requested) != 40 or not set(requested) <= set("0123456789abcdef"):
        raise ValueError("candidate revision requires an exact lowercase 40-character SHA")
    object_type = subprocess.run(
        ["git", "cat-file", "-t", requested], cwd=control, env=env, check=False,
        text=True, capture_output=True,
    )
    if object_type.returncode != 0 or object_type.stdout.strip() != "commit":
        raise ValueError("candidate revision must resolve directly to an available commit object")

    control_values = subprocess.run(
        ["git", "rev-parse", "--show-toplevel", "--path-format=absolute",
         "--git-common-dir", "HEAD"],
        cwd=control, env=env, check=True, text=True, capture_output=True,
    ).stdout.splitlines()
    control_root, common_value, observed_control = control_values
    common = Path(common_value).resolve(strict=True)
    if (Path(control_root).resolve(strict=True) != control
            or common != Path(expected_common).resolve(strict=True)
            or observed_control != control_sha):
        raise ValueError("CONTROL provenance changed after trusted selection")

    values = subprocess.run(
        ["git", "rev-parse", "--show-toplevel", "--path-format=absolute",
         "--git-dir", "--git-common-dir", "HEAD"],
        cwd=candidate, env=env, check=True, text=True, capture_output=True,
    ).stdout.splitlines()
    root_value, git_value, candidate_common, observed = values
    git_dir = Path(git_value).resolve(strict=True)
    if (Path(root_value).resolve(strict=True) != candidate or git_dir == common
            or Path(candidate_common).resolve(strict=True) != common):
        raise ValueError("candidate provenance does not match selected CONTROL")
    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=control, env=env,
        check=True, text=True, capture_output=True,
    ).stdout.splitlines()
    if f"worktree {candidate}" not in worktrees:
        raise ValueError("candidate is not a registered worktree of the requesting repository")

    control_dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=control, env=env, check=True,
        text=True, capture_output=True,
    ).stdout
    candidate_dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=candidate, env=env, check=True,
        text=True, capture_output=True,
    ).stdout
    contains_main = subprocess.run(
        ["git", "merge-base", "--is-ancestor", control_sha, requested],
        cwd=control, env=env, check=False, capture_output=True,
    ).returncode == 0
    if control_dirty:
        raise ValueError("selected CONTROL checkout must remain clean")
    if not contains_main:
        raise ValueError("candidate revision must contain freshly fetched origin/main")
    if candidate_dirty and not allow_dirty_task:
        raise ValueError("candidate must be clean at the exact requested revision")
    observed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=candidate, env=env, check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    if observed != requested:
        raise ValueError("candidate must be clean at the exact requested revision")
    return observed


def clean_environment(source: dict[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in source.items()
        if name != "ASANA_TOKEN"
        and name != MANAGED_NAME
        and name not in AUTHORITY_NAMES
        and not name.startswith("DOCKER_")
    }


def parse_authority(output: str) -> Authority:
    assignments: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, value = line.partition("=")
        if name not in AUTHORITY_NAMES:
            continue
        if not separator or name in assignments:
            raise ValueError("provisioner returned an invalid authority response")
        assignments[name] = value
    if set(assignments) != set(AUTHORITY_NAMES):
        raise ValueError("provisioner returned an invalid authority response")
    references = tuple(
        UUID(value) for value in assignments["REFERENCE_WORK_IDS"].split(",") if value
    )
    return Authority(UUID(assignments["ACTIVE_WORK_ID"]), references)


def provision(
    control: Path, active: str, references: tuple[str, ...], env: dict[str, str]
) -> Authority:
    state_file = str(control / "compose.state.yaml")
    control_file = str(control / "compose.yaml")
    subprocess.run(
        ["docker", "compose", "--project-directory", str(control), "-f", state_file,
         "up", "-d", "--wait", "postgres"],
        cwd=control, env=env, check=True, capture_output=True,
    )
    identity = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "HEAD", "--git-common-dir",
         "--abbrev-ref", "HEAD"],
        cwd=control, env=env, check=True, text=True, capture_output=True,
    ).stdout.splitlines()
    upgrade_env = dict(env)
    if identity[2] == "HEAD":
        upgrade_env.update({
            "SWITCHSTAND_CONTROL_PATH": str(control.resolve()),
            "SWITCHSTAND_CONTROL_SHA": identity[0],
            "SWITCHSTAND_CONTROL_COMMON": identity[1],
        })
    else:
        for name in (
            "SWITCHSTAND_CONTROL_PATH", "SWITCHSTAND_CONTROL_SHA",
            "SWITCHSTAND_CONTROL_COMMON",
        ):
            upgrade_env.pop(name, None)
    upgrade = subprocess.run(
        [str(control / "scripts/switchstand-upgrade-state")],
        cwd=control, env=upgrade_env, text=True, capture_output=True, check=False,
    )
    if upgrade.returncode:
        raise RuntimeError(upgrade.stderr.strip() or "shared state upgrade failed")
    if upgrade.stdout and "; backup " in upgrade.stdout:
        # The backup path is the recovery receipt. Do not swallow it merely
        # because the automatic upgrade succeeded.
        sys.stdout.write(upgrade.stdout)
        sys.stdout.flush()
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(control),
        "-f",
        control_file,
        "run",
        "--build",
        "--rm",
        "--no-deps",
        "controller",
        "uv",
        "run",
        "--no-sync",
        "switchstand-provision",
        "--active",
        asana_task_id(active),
        "--managed-agent",
    ]
    for reference in references:
        command.extend(("--reference", asana_task_id(reference)))
    completed = subprocess.run(
        command, cwd=control, env=env, check=True, text=True, capture_output=True
    )
    return parse_authority(completed.stdout)


def prepare_managed_run(
    control: Path,
    candidate: Path,
    branch: str,
    active: str,
    references: tuple[str, ...],
    env: dict[str, str],
    git_dir: Path,
) -> PreparedRun:
    def reclaim(receipt: RunReceipt) -> None:
        image_name, network_name, database_name = development_names(candidate, receipt.run_id)
        image = inspect_docker("image", image_name, env)
        network = inspect_docker("network", network_name, env)
        database = inspect_docker("container", database_name, env)
        cleanup_development(
            image.object_id if image is not None else None,
            network.object_id if network is not None else None,
            database.object_id if database is not None else None,
            candidate,
            str(receipt.run_id),
            env,
        )

    with reserve_run(candidate, branch, git_dir, reclaim) as record:
        authority = provision(control, active, references, env)
        receipt = record(authority.active)
        development = prepare_development(control, candidate, receipt.run_id, env)
    return PreparedRun(authority, development, receipt)


def supervise_codex(
    command: list[str], env: dict[str, str], development: DevelopmentBoundary, owner: UUID
) -> int:
    forwarded = (
        signal.SIGHUP,
        signal.SIGINT,
        signal.SIGQUIT,
        signal.SIGTERM,
        signal.SIGTSTP,
        signal.SIGCONT,
        signal.SIGWINCH,
    )
    process: subprocess.Popen[bytes] | None = None
    previous_handlers: dict[signal.Signals, Any] = {}
    termination: tuple[int, float] | None = None
    pending: list[int] = []

    def deliver(signum: int) -> None:
        nonlocal termination
        assert process is not None
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
        if signum in {signal.SIGHUP, signal.SIGQUIT, signal.SIGTERM} and termination is None:
            termination = (signum, time.monotonic() + CHILD_TERM_SECONDS)
        if signum == signal.SIGTSTP:
            os.kill(os.getpid(), signal.SIGSTOP)

    def forward(signum: int, _frame: object) -> None:
        if process is None:
            pending.append(signum)
        else:
            deliver(signum)

    try:
        for handled in forwarded:
            previous_handlers[handled] = signal.signal(handled, forward)
        process = subprocess.Popen(command, env=env, start_new_session=True)
        for signum in pending:
            deliver(signum)
        while True:
            try:
                returncode = process.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if termination is not None and time.monotonic() >= termination[1]:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        if termination is not None:
            return 128 + termination[0]
        return returncode if returncode >= 0 else 128 - returncode
    finally:
        try:
            cleanup_development(
                development.image, development.network, development.database,
                Path(env["SWITCHSTAND_WORKTREE"]), str(owner), env
            )
        finally:
            for handled, previous in previous_handlers.items():
                signal.signal(handled, previous)


def run(arguments: argparse.Namespace) -> None:
    control = Path(os.environ["SWITCHSTAND_CONTROL_ROOT"]).resolve(strict=True)
    candidate = Path(os.environ["SWITCHSTAND_CANDIDATE_ROOT"]).resolve(strict=True)
    if Path.cwd().resolve() != control:
        raise ValueError("trusted launcher must execute from CONTROL cwd")
    env = clean_environment(dict(os.environ))
    codex_args = validate_codex_args(arguments.codex_args)
    branch = linked_branch(candidate, env)
    task_branch = "v2-task-" + asana_task_id(arguments.active)
    if branch != task_branch:
        raise ValueError("candidate task branch does not match the exact active task")
    checked = readback(control, candidate, env)
    observed = exact_revision_preflight(
        control, candidate, arguments.commit, os.environ["SWITCHSTAND_CONTROL_SHA"],
        os.environ[REQUESTING_GIT_COMMON], env, allow_dirty_task=True,
    )
    git_dir = Path(subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"], cwd=candidate, env=env,
        check=True, text=True, capture_output=True,
    ).stdout.strip()).resolve(strict=True)
    print(f"Revision: {observed}", file=sys.stderr)
    prepared = prepare_managed_run(
        control, candidate, branch, arguments.active, tuple(arguments.reference), env, git_dir
    )
    authority, development, receipt = prepared
    env["ACTIVE_WORK_ID"] = str(authority.active)
    env["REFERENCE_WORK_IDS"] = ",".join(map(str, authority.references))
    env["SWITCHSTAND_MANAGED"] = "1"
    env["SWITCHSTAND_WORKTREE"] = str(candidate)
    env["SWITCHSTAND_CONTROL_ROOT"] = str(control)
    env["SWITCHSTAND_BRANCH"] = branch
    env["SWITCHSTAND_GIT_COMMON"] = str(Path(subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=candidate,
        env=env, check=True, text=True, capture_output=True).stdout.strip()).resolve())
    env["SWITCHSTAND_GIT_DIR"] = str(git_dir)
    env["SWITCHSTAND_QUALITY_IMAGE"] = development.image
    env["SWITCHSTAND_QUALITY_NETWORK"] = development.network
    env["SWITCHSTAND_DATABASE_CONTAINER"] = development.database
    env["SWITCHSTAND_RUN_ID"] = str(receipt.run_id)
    env["SWITCHSTAND_MANIFEST_SHA256"] = development.manifest
    print(f"Codex profile: {checked.profile} ({checked.sandbox})", file=sys.stderr)
    print(f"Run: {receipt.run_id}", file=sys.stderr)
    print("Instruction sources: " + ", ".join(checked.instruction_sources), file=sys.stderr)
    command = codex_command(control, candidate, codex_args)
    raise SystemExit(supervise_codex(command, env, development, receipt.run_id))


def main() -> None:
    arguments = parser().parse_args()
    try:
        run(arguments)
    except (KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser().exit(1, f"launch failed: {error}\n")


if __name__ == "__main__":
    main()
