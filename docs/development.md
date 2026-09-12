# Development

- Bootstrap/build: install Docker with Compose, then `docker compose build controller`.
- Stable host tools: run `scripts/bootstrap`. The script reuses a lockfile-fingerprinted `.venv` shared by linked
  worktrees and obtains the pinned uv binary through Docker only when needed.
- Full clean/container quality runs in CI. Run `docker compose run --build --rm quality` locally only when changing
  Docker, runtime, or development-tool behavior that CI cannot qualify for the host.
- Services: start stable state with `docker compose -f compose.state.yaml up -d --wait`, then run
  `docker compose up --build`.
- Migration: `uv run alembic upgrade head`
- Writer worktree: from the ordinary checkout, run
  `scripts/switchstand-worktree <writer-name> <exact-40-character-green-SHA>`, then work from the printed path. The
  helper fails closed on an existing target or branch and records the exact launch baseline in linked-worktree Git
  metadata; one managed writer owns each linked worktree. Launch refuses a dirty or advanced baseline.
- Setup once: run `install -d -m 700 ~/.config/switchstand` and
  `install -m 600 switchstand-config.example ~/.config/switchstand/.env`, then fill in `ASANA_TOKEN`. This file is stable machine
  configuration; never put per-run work authority in it.
- Managed Codex: run `scripts/switchstand-launch --active <Asana task URL> --reference <reference URL>`. Task IDs work
  too, and up to eight `--reference` arguments are accepted. The launcher binds those human-readable tasks, injects
  their opaque handles for this process only, verifies the bounded `switchstand-development` profile and loaded
  instruction sources, then replaces itself with Codex. It refuses the ordinary checkout. Do not copy WorkIds or edit
  the shared environment file. At launch it pins a development image and starts a writer-local internal test network;
  the agent receives exact `quality`, `commit_all_current_worktree`, and read-only `run_status` tools. Ordinary commands cannot reach the
  Docker socket or shared Git metadata, and the quality tool never evaluates worktree-edited Docker instructions.
  During implementation, `scripts/check <affected-test-paths>` runs Ruff, strict Pyright, and affected tests from the stable host
  environment. CI supplies the routine full clean/container gate; real-host launch and recovery changes still require
  a real-host canary.
  Focused checks stop after 120 seconds instead of silently falling back to repeated container rebuilds. Selecting a
  database-backed test without a live `TEST_DATABASE_URL` fails rather than skips.
  Each launch writes a protected per-worktree run receipt with a fresh identity and Linux PID start token. The
  read-only `run_status` tool reports `running`, `stopped`, `lost`, or `unknown` without accepting an arbitrary PID.
  From that linked worktree, `scripts/switchstand-run-stop` uses the repository's bootstrapped Python environment and
  stops only the receipt's exact process identity: pidfd-pinned `SIGTERM`, a fixed bounded wait, then pidfd-pinned
  `SIGKILL`. It returns `lost` or `unknown` without signalling when identity cannot be proven, and never accepts a PID,
  signal, timeout, path, or process group.
- One-step managed start: from the clean ordinary `main` checkout at the locally accepted `origin/main`, run
  `scripts/switchstand-start --active <Asana task URL>`. It verifies that exact baseline, creates the task-named linked
  writer worktree, and replaces itself with the managed launcher. Task IDs, references, and one optional prompt are
  forwarded unchanged.
- Bounded candidate qualification is a separate, explicit host operation. From clean ordinary `main` at the locally
  accepted `origin/main`, run `scripts/switchstand-qualify --candidate <exact-40-character-SHA> --active <authorized
  disposable task> --approved <read-only governing task>`; repeat `--approved` for up to eight references. The
  candidate must already be an available descendant of accepted main and contain the reviewed six-tool canary.
  Qualification attaches it as a clean detached worktree, builds and labels the exact image, and starts a unique
  tmpfs PostgreSQL container whose only database is `switchstand_test`. The host wrapper obtains `ASANA_TOKEN` from
  the protected owner-only config and supplies the test database URL only inside the candidate server container;
  neither value enters the Codex process, project MCP config, or command sandbox. Before Codex starts, the launcher
  reads back the exact six-tool inventory, closed schemas, grant, opaque WorkIds, and active/reference source IDs.
  The qualification profile is read-only with network and Docker socket access disabled, and the ordinary controller
  and development MCP servers are disabled for that session. Candidate/server/database/network/image-tag/worktree
  mismatch or cleanup uncertainty fails the run closed. This mode never moves `main`, candidate branches, pull
  requests, shared state, or production services; `scripts/switchstand-start` remains the ordinary development path.
- MCP server: `docker compose run --rm -T controller`
- Codex: the checked-in project MCP configuration launches the same required STDIO server. It forwards only `HOME`
  and the launch-scoped opaque authority; Compose obtains the provider credential from the protected shared file.
  The command sandbox cannot read that file. Direct `codex` invocation is not the supported managed path.
  A fresh head calls `work_get` with only `api_version="1"`; the tool defaults to its active assignment and advertises
  the opaque IDs of its bounded read-only references.

CI runs on Python 3.14 with PostgreSQL. Correctness, types, and tests block; formatting is reported without rewriting
review diffs. Stage branches and pull requests are based on the exact last accepted green SHA. Integration admits
State, Provider, then MCP and reruns affected plus full gates after each admission.

Canonical cumulative handwritten Python LOC is counted with
`git ls-files src tests | rg '\.py$' | xargs wc -l`; generated files and dependencies are excluded.
