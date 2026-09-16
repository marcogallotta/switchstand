import argparse
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

from .launch import clean_environment, provision
from .task_ref import asana_task_id


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Start development in a private task clone.")
    result.add_argument("--active", required=True, help="active Asana task URL or ID")
    return result


def _git(repo: Path, *arguments: str, env: dict[str, str]) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments], capture_output=True, text=True,
        check=True, env=env,
    ).stdout.strip()


def _private_directory(path: Path, *, create: bool = True) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise ValueError(f"{path} must be a real user-owned 0700 directory")
    return path.resolve(strict=True)


def validate_control(control: Path, env: dict[str, str]) -> Path:
    control = control.resolve(strict=True)
    root, git_dir, common, branch, head = _git(
        control, "rev-parse", "--path-format=absolute", "--show-toplevel",
        "--git-dir", "--git-common-dir", "--abbrev-ref", "HEAD", "HEAD", env=env,
    ).splitlines()
    if Path(root).resolve() != control:
        raise ValueError("launcher must run from the clean CONTROL checkout root")
    if _git(control, "status", "--porcelain", "--untracked-files=all", env=env):
        raise ValueError("launcher refuses dirty CONTROL; local work is intact")
    primary = Path(common).resolve().parent
    if Path(git_dir).resolve() == Path(common).resolve():
        if control != primary or branch != "main":
            raise ValueError("ordinary CONTROL must be the main checkout on branch main")
        if head != _git(control, "rev-parse", "origin/main", env=env):
            raise ValueError("ordinary CONTROL must match the locally accepted origin/main")
    elif branch != "HEAD":
        raise ValueError("non-primary CONTROL must be detached at one exact revision")
    active_hook = primary / "scripts/codex-hook"
    control_hook = control / "scripts/codex-hook"
    if not active_hook.is_file() or active_hook.read_bytes() != control_hook.read_bytes():
        raise ValueError("exact CONTROL hook is not the accepted Switchstand hook")
    return control


def _provider_origin(control: Path, env: dict[str, str]) -> str:
    origin = _git(control, "remote", "get-url", "origin", env=env)
    if origin.startswith(("/", "file:")) or origin in {".", ".."}:
        raise ValueError("CONTROL origin must be an external provider repository")
    return origin


def durable_root(env: dict[str, str]) -> Path:
    return _private_directory(Path(env["HOME"]) / ".local/state/switchstand/writers")


def validate_writer(control: Path, writer: Path, active: str, env: dict[str, str]) -> Path:
    writer = writer.resolve(strict=True)
    task = asana_task_id(active)
    if writer != durable_root(env) / f"task-{task}":
        raise ValueError("writer is not the canonical private clone for this task")
    _private_directory(writer, create=False)
    git_dir = _private_directory(writer / ".git", create=False)
    root, absolute_git, common, branch, head = _git(
        writer, "rev-parse", "--path-format=absolute", "--show-toplevel",
        "--git-dir", "--git-common-dir", "--abbrev-ref", "HEAD", "HEAD", env=env,
    ).splitlines()
    if (Path(root).resolve() != writer or Path(absolute_git).resolve() != git_dir
            or Path(common).resolve() != git_dir or branch != f"v2-task-{task}"):
        raise ValueError("writer does not have private task-bound Git metadata")
    if (git_dir / "objects/info/alternates").exists():
        raise ValueError("writer must not share an object store")
    if (git_dir / "switchstand-active-task").read_text().strip() != task:
        raise ValueError("writer task binding does not match")
    green = (git_dir / "switchstand-green-sha").read_text().strip()
    if subprocess.run(
        ["git", "-C", str(writer), "merge-base", "--is-ancestor", green, head],
        env=env, capture_output=True, check=False,
    ).returncode:
        raise ValueError("writer does not descend from its green baseline")
    if _git(writer, "remote", "get-url", "origin", env=env) != _provider_origin(control, env):
        raise ValueError("writer origin is not the exact provider repository")
    return writer


def create_writer(control: Path, active: str, env: dict[str, str]) -> Path:
    task = asana_task_id(active)
    writer = durable_root(env) / f"task-{task}"
    if writer.exists():
        return validate_writer(control, writer, active, env)
    branch = f"v2-task-{task}"
    base = _git(control, "rev-parse", "HEAD", env=env)
    subprocess.run(
        ["git", "clone", "--no-local", "--no-checkout", str(control), str(writer)],
        check=True, env=env, capture_output=True, text=True,
    )
    writer.chmod(0o700)
    (writer / ".git").chmod(0o700)
    _git(writer, "remote", "set-url", "origin", _provider_origin(control, env), env=env)
    _git(writer, "checkout", "-b", branch, base, env=env)
    git_dir = writer / ".git"
    (git_dir / "switchstand-active-task").write_text(task + "\n")
    (git_dir / "switchstand-green-sha").write_text(base + "\n")
    for marker in (git_dir / "switchstand-active-task", git_dir / "switchstand-green-sha"):
        marker.chmod(0o600)
    return validate_writer(control, writer, active, env)


def _validate_auth(home: Path) -> Path:
    auth = home / ".codex/auth.json"
    metadata = auth.lstat()
    if (stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600
            or auth.resolve(strict=True) != auth.absolute()):
        raise ValueError("canonical Codex auth must be a real user-owned 0600 file")
    return auth


def managed_codex_home(control: Path, writer: Path, active: str, env: dict[str, str]) -> Path:
    task = asana_task_id(active)
    root = _private_directory(Path(env["HOME"]) / ".local/state/switchstand/codex")
    managed = _private_directory(root / f"task-{task}")
    auth = _validate_auth(Path(env["HOME"]))
    link = managed / "auth.json"
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve(strict=True) != auth:
            raise ValueError("managed Codex auth binding is invalid")
    else:
        link.symlink_to(auth)
    hook = control / "scripts/codex-hook"
    executable = shutil.which("codex", path=env.get("PATH"))
    if executable is None:
        raise ValueError("Codex executable is unavailable")
    codex_executable = Path(executable).resolve(strict=True)
    hooks = {"description": "Exact clean-CONTROL Switchstand guard.", "hooks": {
        "PreToolUse": [{"matcher": "^Bash$", "hooks": [
            {"type": "command", "command": str(hook), "timeout": 10}]}],
        "PermissionRequest": [{"matcher": "^Bash$", "hooks": [
            {"type": "command", "command": str(hook), "timeout": 10}]}],
    }}
    (managed / "hooks.json").write_text(json.dumps(hooks, indent=2) + "\n")
    config = f'''approval_policy = "never"
default_permissions = "switchstand-task"

[features]
hooks = true

[projects."{writer}"]
trust_level = "untrusted"

[permissions.switchstand-task.filesystem]
glob_scan_max_depth = 4
":minimal" = "read"
"{codex_executable}" = "read"
"{managed}" = "deny"
"{auth}" = "deny"
"/var/run/docker.sock" = "deny"

[permissions.switchstand-task.workspace_roots]
"{writer}" = true

[permissions.switchstand-task.filesystem.":workspace_roots"]
"." = "write"
".codex" = "read"
".env" = "deny"
"**/.env" = "deny"
".env.local" = "deny"
"**/.env.local" = "deny"

[permissions.switchstand-task.network]
enabled = true

[shell_environment_policy]
inherit = "all"
ignore_default_excludes = false
exclude = ["*TOKEN*", "*SECRET*", "*PASSWORD*", "*CREDENTIAL*", "SSH_AUTH_SOCK", "GIT_ASKPASS", "GH_*", "DOCKER_CONFIG"]
'''
    (managed / "config.toml").write_text(config)
    (managed / "config.toml").chmod(0o600)
    (managed / "hooks.json").chmod(0o600)
    return managed


def codex_command(control: Path, writer: Path) -> list[str]:
    prompt = ('Load the exact launch-bound context with work_get(api_version="1") '
              "without a WorkId before material work. Work only in this private task "
              "clone. This is ordinary development; the exact CONTROL hook remains active.")
    return [
        "codex", "-C", str(writer), "-a", "never", "--dangerously-bypass-hook-trust",
        "-c", f'mcp_servers.switchstand.command="{control / "scripts/switchstand-context-mcp"}"',
        "-c", 'mcp_servers.switchstand.env_vars=["HOME","SWITCHSTAND_MANAGED","ACTIVE_WORK_ID"]',
        "-c", 'mcp_servers.switchstand.enabled_tools=["work_get"]',
        "-c", 'mcp_servers.switchstand.tools.work_get.approval_mode="approve"',
        "-c", "mcp_servers.switchstand.required=true",
        prompt,
    ]


def run(active: str) -> None:
    env = clean_environment(dict(os.environ))
    control = validate_control(Path.cwd(), env)
    authority = provision(control, active, (), env)
    writer = create_writer(control, active, env)
    env["ACTIVE_WORK_ID"] = str(authority.active)
    env["SWITCHSTAND_MANAGED"] = "1"
    env["CODEX_HOME"] = str(managed_codex_home(control, writer, active, env))
    for name in tuple(env):
        upper = name.upper()
        if (any(word in upper for word in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))
                or name in {"SSH_AUTH_SOCK", "GIT_ASKPASS", "DOCKER_CONFIG"}
                or name.startswith("GH_")):
            env.pop(name, None)
    os.execvpe("codex", codex_command(control, writer), env)


def main() -> None:
    arguments = parser().parse_args()
    try:
        run(arguments.active)
    except (KeyError, ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        parser().exit(1, f"switchstand launch failed: {error}\n")


if __name__ == "__main__":
    main()
