# Development

- Bootstrap/build: `docker compose build controller`
- Host install/sync (optional): `uv sync --locked --all-extras --dev`
- Quality: `docker compose run --build --rm quality`
- Services: `docker compose up --build`
- Migration: `uv run alembic upgrade head`
- Writer worktree: from the ordinary checkout, run
  `scripts/switchstand-worktree <writer-name> <exact-40-character-green-SHA>`, then work from the printed path. The
  helper fails closed on an existing target or branch; one managed writer owns each linked worktree.
- Setup once: run `install -d -m 700 ~/.config/switchstand` and
  `install -m 600 .env.example ~/.config/switchstand/.env`, then fill in `ASANA_TOKEN`. This file is stable machine
  configuration; never put per-run work authority in it.
- Managed Codex: run `scripts/switchstand-launch --active <Asana task URL> --reference <reference URL>`. Task IDs work
  too, and up to eight `--reference` arguments are accepted. The launcher binds those human-readable tasks, injects
  their opaque handles for this process only, verifies the bounded `switchstand-development` profile and loaded
  instruction sources, then replaces itself with Codex. It refuses the ordinary checkout. Do not copy WorkIds or edit
  the shared environment file.
- MCP server: `docker compose run --rm -T controller`
- Codex: the checked-in project MCP configuration launches the same required STDIO server. It forwards only `HOME`
  and the launch-scoped opaque authority; Compose obtains the provider credential from the protected shared file.
  The command sandbox cannot read that file. Direct `codex` invocation is not the supported managed path.
  A fresh head calls `work_get` with only `api_version="1"`; the tool defaults to its active assignment and advertises
  the opaque IDs of its bounded read-only references.

CI runs on Python 3.14 with PostgreSQL. Correctness, types, and tests block; formatting is reported without rewriting
review diffs. Stage branches and pull requests are based on the exact last accepted green SHA. Integration admits
State, Provider, then MCP and reruns affected plus full gates after each admission.

Canonical handwritten Python LOC is counted from tracked `src/**/*.py` and `tests/**/*.py` files with
`git ls-files 'src/**/*.py' 'tests/**/*.py' | xargs wc -l`; generated files and dependencies are excluded.
