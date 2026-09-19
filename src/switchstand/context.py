import argparse
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from .launch import clean_environment, provision
from .session import supervise
from .task_ref import asana_task_id


def initial_assignment(value: str) -> str:
    if not value:
        raise argparse.ArgumentTypeError("initial assignment must not be empty")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Start development in a private task clone.")
    result.add_argument("--active", required=True, help="active Asana task URL or ID")
    result.add_argument(
        "assignment", nargs=1, type=initial_assignment,
        help="exact initial assignment (pass after --)",
    )
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
    _git(control, "fetch", "--no-tags", "origin",
         "+refs/heads/main:refs/remotes/origin/main", env=env)
    accepted = _git(control, "rev-parse", "origin/main", env=env)
    identity = f"CONTROL {control}: HEAD={head}; origin/main={accepted}"
    if _git(control, "status", "--porcelain", "--untracked-files=all", env=env):
        raise ValueError(f"{identity}; dirty CONTROL; local work is intact")
    primary = Path(common).resolve().parent
    if Path(git_dir).resolve() == Path(common).resolve():
        if control != primary or branch != "main":
            raise ValueError("ordinary CONTROL must be the main checkout on branch main")
        if head != accepted:
            _fast_forward(control, head, accepted, identity, env)
    elif branch != "HEAD":
        raise ValueError("non-primary CONTROL must be detached at one exact revision")
    elif head != accepted:
        _fast_forward(control, head, accepted, identity, env)
    control_hook = control / "scripts/codex-hook"
    if (not control_hook.is_file() or not os.access(control_hook, os.X_OK)
            or control_hook.resolve(strict=True) != control / "scripts/codex-hook"):
        raise ValueError("exact CONTROL hook is unavailable")
    return control


def _fast_forward(
    repo: Path, head: str, accepted: str, identity: str, env: dict[str, str],
) -> None:
    if subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", head, accepted],
        env=env, capture_output=True, check=False,
    ).returncode:
        raise ValueError(f"{identity}; not a clean ancestor; local work is intact")
    _git(repo, "merge", "--ff-only", accepted, env=env)
    if (_git(repo, "rev-parse", "HEAD", env=env) != accepted
            or _git(repo, "status", "--porcelain", "--untracked-files=all", env=env)):
        raise ValueError(f"{identity}; fast-forward readback failed")


def _provider_origin(control: Path, env: dict[str, str]) -> str:
    origin = _git(control, "remote", "get-url", "origin", env=env)
    parsed = urlparse(origin)
    provider_url = parsed.scheme in {"https", "ssh", "git"} and bool(parsed.netloc)
    provider_scp = (":" in origin and "@" in origin.split(":", 1)[0]
                    and not origin.startswith(("/", "./", "../")))
    if not (provider_url or provider_scp):
        raise ValueError("CONTROL origin must be an external provider repository")
    return origin


def durable_root(env: dict[str, str]) -> Path:
    return _private_directory(Path(env["HOME"]) / ".local/state/switchstand/writers")


def _bind_git_identity(control: Path, writer: Path, env: dict[str, str]) -> None:
    # The task session cannot read the user's global Git configuration. Bind the
    # ordinary author identity into this private clone while CONTROL can still
    # resolve it, so local commits do not require broader HOME access.
    for key in ("user.name", "user.email"):
        _git(writer, "config", "--local", key,
             _git(control, "config", "--get", key, env=env), env=env)


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
    root = durable_root(env)
    writer = root / f"task-{task}"
    if writer.exists():
        validated = validate_writer(control, writer, active, env)
        head = _git(validated, "rev-parse", "HEAD", env=env)
        green_path = validated / ".git/switchstand-green-sha"
        green = green_path.read_text().strip()
        accepted = _git(control, "rev-parse", "HEAD", env=env)
        identity = f"writer {validated}: HEAD={head}; accepted main={accepted}"
        dirty = _git(validated, "status", "--porcelain", "--untracked-files=all", env=env)
        # A writer with task progress is recovery state, not a stale checkout.
        # Resume it exactly. Only an untouched writer (HEAD still at its recorded
        # green baseline and no dirty files) may follow a newer accepted main.
        if not dirty and head == green and head != accepted:
            _git(validated, "fetch", "--no-tags", "--no-write-fetch-head",
                 str(control), accepted, env=env)
            _fast_forward(validated, head, accepted, identity, env)
            green_path.write_text(accepted + "\n")
        _bind_git_identity(control, validated, env)
        return validated
    branch = f"v2-task-{task}"
    base = _git(control, "rev-parse", "HEAD", env=env)
    temporary = Path(tempfile.mkdtemp(prefix=f".task-{task}.", dir=root))
    try:
        subprocess.run(
            ["git", "clone", "--no-local", "--no-checkout", str(control), str(temporary)],
            check=True, env=env, capture_output=True, text=True,
        )
        temporary.chmod(0o700)
        (temporary / ".git").chmod(0o700)
        _git(temporary, "remote", "set-url", "origin", _provider_origin(control, env), env=env)
        _git(temporary, "checkout", "-b", branch, base, env=env)
        _bind_git_identity(control, temporary, env)
        git_dir = temporary / ".git"
        (git_dir / "switchstand-active-task").write_text(task + "\n")
        (git_dir / "switchstand-green-sha").write_text(base + "\n")
        for marker in (git_dir / "switchstand-active-task", git_dir / "switchstand-green-sha"):
            marker.chmod(0o600)
        temporary.rename(writer)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return validate_writer(control, writer, active, env)


def _validate_auth(home: Path) -> Path:
    auth = home / ".codex/auth.json"
    metadata = auth.lstat()
    if (stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600
            or auth.resolve(strict=True) != auth.absolute()):
        raise ValueError("canonical Codex auth must be a real user-owned 0600 file")
    return auth


def _write_managed(path: Path, content: str) -> None:
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if (stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()):
            raise ValueError(f"managed file {path} is unsafe")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def prepared_check_environment(control: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        [str(control / "scripts/bootstrap"), "--print-uv"],
        env=env, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip())
    path = Path(result.stdout.strip())
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("invalid pinned uv binding; run scripts/bootstrap in the primary checkout")
    return str(path.resolve(strict=True))


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
    _write_managed(managed / "hooks.json", json.dumps(hooks, indent=2) + "\n")
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
"{env["SWITCHSTAND_CHECK_UV"]}" = "read"
"{managed}" = "deny"
"{auth}" = "deny"
"/var/run/docker.sock" = "deny"

[permissions.switchstand-task.workspace_roots]
"{writer}" = true

[permissions.switchstand-task.filesystem.":workspace_roots"]
"." = "write"
".git" = "write"
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
    _write_managed(managed / "config.toml", config)
    return managed


def codex_command(control: Path, writer: Path, assignment: str) -> list[str]:
    if not assignment:
        raise ValueError("initial assignment must not be empty")
    prompt = ('Exact launch assignment:\n' + assignment + '\n\n'
              'Ground this assignment with work_get(api_version="1") '
              "without a WorkId, then reconcile its current history with "
              "work_history(api_version=\"1\", observed_revision=<the returned revision>) "
              "before material work. Follow next_cursor until null; if history is stale, "
              "repeat work_get and restart the history read. Do not resume completed or "
              "superseded intent. Work only in this private task clone. This is ordinary "
              "development; the exact CONTROL hook remains active.")
    return [
        "codex", "-C", str(writer), "-m", "gpt-5.6-sol", "-a", "never",
        "--dangerously-bypass-hook-trust",
        "-c", f'mcp_servers.switchstand.command="{control / "scripts/switchstand-context-mcp"}"',
        "-c", 'mcp_servers.switchstand.env_vars=["HOME","SWITCHSTAND_MANAGED","ACTIVE_WORK_ID"]',
        "-c", 'mcp_servers.switchstand.enabled_tools=["work_get","work_history"]',
        "-c", 'mcp_servers.switchstand.default_tools_approval_mode="auto"',
        "-c", 'mcp_servers.switchstand.tools.work_get.approval_mode="auto"',
        "-c", 'mcp_servers.switchstand.tools.work_history.approval_mode="auto"',
        "-c", "mcp_servers.switchstand.required=true",
        prompt,
    ]


def run(active: str, assignment: str) -> None:
    env = clean_environment(dict(os.environ))
    loaded_head = _git(Path.cwd(), "rev-parse", "HEAD", env=env)
    control = validate_control(Path.cwd(), env)
    if _git(control, "rev-parse", "HEAD", env=env) != loaded_head:
        # Load the accepted launcher's code before using any shared services.
        os.execv(str(control / "scripts/switchstand"),
                 [str(control / "scripts/switchstand"), "--active", active,
                  "--", assignment])
    env["SWITCHSTAND_CHECK_UV"] = prepared_check_environment(control, env)
    writer = create_writer(control, active, env)
    authority = provision(control, active, (), env)
    env["ACTIVE_WORK_ID"] = str(authority.active)
    env["SWITCHSTAND_MANAGED"] = "1"
    env["SWITCHSTAND_TASK_WRITER"] = str(writer)
    env["SWITCHSTAND_TASK_ID"] = asana_task_id(active)
    env["CODEX_HOME"] = str(managed_codex_home(control, writer, active, env))
    for name in tuple(env):
        upper = name.upper()
        if (any(word in upper for word in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))
                or name in {"SSH_AUTH_SOCK", "GIT_ASKPASS", "DOCKER_CONFIG"}
                or name.startswith("GH_")):
            env.pop(name, None)
    raise SystemExit(supervise(codex_command(control, writer, assignment), env, writer / ".git"))


def main() -> None:
    arguments = parser().parse_args()
    try:
        run(arguments.active, arguments.assignment[0])
    except (KeyError, ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        parser().exit(1, f"switchstand launch failed: {error}\n")


if __name__ == "__main__":
    main()
