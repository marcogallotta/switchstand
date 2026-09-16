import argparse
import os
import stat
import subprocess
from pathlib import Path

from .launch import clean_environment, provision
from .task_ref import asana_task_id


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Start a task-bound Codex in its durable Switchstand writer."
    )
    result.add_argument("--active", required=True, help="active Asana task URL or ID")
    result.add_argument(
        "--writer",
        type=Path,
        help="adopt one existing durable Switchstand linked writer for this exact task",
    )
    return result


def codex_command(control: Path, writer: Path) -> list[str]:
    prompt = (
        'Load the exact launch-bound context with work_get(api_version="1") without '
        "a WorkId before material work. Work only inside this exact task writer. This "
        "is ordinary development: use its normal Git, network, test, review, and landing "
        "workflow within the standing assignment; do not mutate the primary checkout."
    )
    return [
        "codex",
        "-C",
        str(writer),
        "-a",
        "never",
        "-s",
        "danger-full-access",
        "--dangerously-bypass-hook-trust",
        "-c",
        f'mcp_servers.switchstand.command="{control / "scripts" / "switchstand-context-mcp"}"',
        "-c",
        'mcp_servers.switchstand.env_vars=["HOME","SWITCHSTAND_MANAGED","ACTIVE_WORK_ID"]',
        "-c",
        'mcp_servers.switchstand.enabled_tools=["work_get"]',
        "-c",
        "mcp_servers.switchstand.required=true",
        prompt,
    ]


def _git(repo: Path, *arguments: str, env: dict[str, str]) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()


def validate_control(control: Path, env: dict[str, str]) -> Path:
    control = control.resolve(strict=True)
    root, git_dir, common, branch, head = _git(
        control,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-dir",
        "--git-common-dir",
        "--abbrev-ref",
        "HEAD",
        "HEAD",
        env=env,
    ).splitlines()
    if Path(root).resolve() != control:
        raise ValueError("launcher must run from the clean CONTROL checkout root")
    if _git(control, "status", "--porcelain", "--untracked-files=all", env=env):
        raise ValueError("launcher refuses dirty CONTROL; local work is intact")
    primary = Path(common).resolve().parent
    if Path(git_dir).resolve() == Path(common).resolve():
        if control != primary or branch != "main":
            raise ValueError("ordinary CONTROL must be the main checkout on branch main")
        remote = _git(control, "rev-parse", "origin/main", env=env)
        if head != remote:
            raise ValueError("ordinary CONTROL must match the locally accepted origin/main")
    elif branch != "HEAD":
        raise ValueError("non-primary CONTROL must be detached at one exact revision")
    active_hook = primary / "scripts" / "codex-hook"
    control_hook = control / "scripts" / "codex-hook"
    if not active_hook.is_file() or active_hook.read_bytes() != control_hook.read_bytes():
        raise ValueError("exact CONTROL hook is not the active Switchstand hook")
    return control


def durable_root(env: dict[str, str]) -> Path:
    root = Path(env["HOME"]) / ".local/state/switchstand/worktrees"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = root.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("durable Switchstand writer root must be a real user-owned 0700 directory")
    return root.resolve(strict=True)


def validate_writer(
    control: Path, writer: Path, active: str, env: dict[str, str]
) -> Path:
    writer = writer.resolve(strict=True)
    state_root = durable_root(env)
    if writer.parent != state_root:
        raise ValueError("writer must be a direct child of the durable Switchstand worktree root")
    root, git_dir, common, branch, head = _git(
        writer,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-dir",
        "--git-common-dir",
        "--abbrev-ref",
        "HEAD",
        "HEAD",
        env=env,
    ).splitlines()
    control_common = Path(_git(control, "rev-parse", "--path-format=absolute", "--git-common-dir", env=env)).resolve()
    if (
        Path(root).resolve() != writer
        or Path(git_dir).resolve() == Path(common).resolve()
        or Path(common).resolve() != control_common
        or branch == "HEAD"
        or not branch.startswith("v2-")
    ):
        raise ValueError("writer is not an owned linked Switchstand worktree")
    records = _git(control, "worktree", "list", "--porcelain", env=env).splitlines()
    if f"worktree {writer}" not in records:
        raise ValueError("writer is not registered to the requesting Switchstand repository")
    green_path = Path(git_dir) / "switchstand-green-sha"
    green = green_path.read_text().strip()
    if (
        len(green) != 40
        or any(character not in "0123456789abcdef" for character in green)
        or subprocess.run(
            ["git", "-C", str(writer), "cat-file", "-e", f"{green}^{{commit}}"],
            env=env,
            capture_output=True,
            check=False,
        ).returncode
        or subprocess.run(
            ["git", "-C", str(writer), "merge-base", "--is-ancestor", green, head],
            env=env,
            capture_output=True,
            check=False,
        ).returncode
    ):
        raise ValueError("writer does not descend from its recorded green baseline")
    task = asana_task_id(active)
    task_path = Path(git_dir) / "switchstand-active-task"
    if task_path.is_file():
        if task_path.read_text().strip() != task:
            raise ValueError("writer is already bound to a different active task")
    elif writer.name == f"switchstand-work-{task}" and branch == f"v2-work-{task}":
        descriptor = os.open(task_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(task + "\n")
    else:
        raise ValueError("existing writer has no exact binding to the requested task")
    return writer


def create_writer(
    repo: Path, active: str, env: dict[str, str], existing: Path | None = None
) -> Path:
    if existing is not None:
        return validate_writer(repo, existing, active, env)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()
    completed = subprocess.run(
        [
            str(repo / "scripts" / "switchstand-worktree"),
            f"work-{asana_task_id(active)}",
            head,
            "--resume-work",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo,
        env=env,
    )
    return validate_writer(repo, Path(completed.stdout.strip()), active, env)


def run(active: str, existing: Path | None = None) -> None:
    env = clean_environment(dict(os.environ))
    control = validate_control(Path.cwd(), env)
    authority = provision(control, active, (), env)
    writer = create_writer(control, active, env, existing)
    env["ACTIVE_WORK_ID"] = str(authority.active)
    env["SWITCHSTAND_MANAGED"] = "1"
    os.execvpe("codex", codex_command(control, writer), env)


def main() -> None:
    arguments = parser().parse_args()
    try:
        run(arguments.active, arguments.writer)
    except (KeyError, ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        parser().exit(1, f"switchstand launch failed: {error}\n")


if __name__ == "__main__":
    main()
