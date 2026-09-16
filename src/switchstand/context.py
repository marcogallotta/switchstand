import argparse
import os
from pathlib import Path

from .launch import clean_environment, provision


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Start Codex with read-only context from one exact Asana task."
    )
    result.add_argument("--active", required=True, help="active Asana task URL or ID")
    return result


def codex_command(repo: Path) -> list[str]:
    prompt = (
        'Load the exact launch-bound context with work_get(api_version="1") without '
        "a WorkId before material work. The tool is read-only; the task source/history "
        "and feedback or development tools are not available in this session."
    )
    return [
        "codex",
        "-C",
        str(repo),
        "-c",
        'mcp_servers.switchstand.command="scripts/switchstand-context-mcp"',
        "-c",
        'mcp_servers.switchstand.env_vars=["HOME","SWITCHSTAND_MANAGED","ACTIVE_WORK_ID"]',
        "-c",
        'mcp_servers.switchstand.enabled_tools=["work_get"]',
        "-c",
        "mcp_servers.switchstand.required=true",
        "-c",
        "mcp_servers.switchstand_development.enabled=false",
        prompt,
    ]


def run(active: str) -> None:
    repo = Path.cwd().resolve(strict=True)
    env = clean_environment(dict(os.environ))
    authority = provision(repo, active, (), env)
    env["ACTIVE_WORK_ID"] = str(authority.active)
    env["SWITCHSTAND_MANAGED"] = "1"
    os.execvpe("codex", codex_command(repo), env)


def main() -> None:
    arguments = parser().parse_args()
    try:
        run(arguments.active)
    except (KeyError, ValueError, RuntimeError, OSError) as error:
        parser().exit(1, f"context launch failed: {error}\n")


if __name__ == "__main__":
    main()
