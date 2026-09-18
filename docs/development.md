# Development

- Bootstrap/build: install Docker with Compose, then `docker compose build controller`.
- Stable host tools: run `scripts/bootstrap`. The script reuses a lockfile-fingerprinted `.venv` shared by linked
  worktrees and obtains the pinned uv binary through Docker only when needed.
- Full clean/container quality runs in CI. Run `docker compose run --build --rm quality` locally only when changing
  Docker, runtime, or development-tool behavior that CI cannot qualify for the host.
- For focused Docker, runtime, or tooling validation, run the normal image build so Docker validates and reuses its
  cache. Record the resulting immutable image ID together with the candidate SHA and dirty state; a dirty worktree is
  not an immutable candidate. Never reuse an arbitrary tag or an image of unknown provenance. When appropriate, mount
  the current worktree read-only and set `PYTHONPATH` to its `src` for affected tests, but report that separately from
  clean image-contained or full-integration evidence. CI supplies the routine clean/full gate; rerun locally only the
  causal gates affected by the change or boundary being claimed.
- Services: start stable state with `docker compose -f compose.state.yaml up -d --wait`, then run
  `docker compose up --build`.
- Migration: `uv run alembic upgrade head`

Shared state upgrades use `scripts/switchstand-upgrade-state` from clean, current
`main`, after stopping Switchstand writers. The command refuses concurrent
execution, other database clients, or an unexpected service, volume, or schema revision;
creates a private custom-format dump under
`~/.local/state/switchstand/backups`; restores and upgrades that dump in a
disposable PostgreSQL instance; and only then upgrades shared state. It reads
back the exact new revision and preserved pre-existing table counts. Keep the
reported dump until the upgraded service has been exercised successfully.

The command deliberately does not downgrade or auto-restore after an ambiguous
failure. Stop Switchstand writers, preserve the dump and command output, and
diagnose before any recovery attempt. A restore is a separate reviewed operator
action.
- Writer worktree: from the ordinary checkout, run
  `scripts/switchstand-worktree <writer-name> <exact-40-character-green-SHA>`, then work from the printed path. The
  helper creates a new linked worktree when the target is absent. If the target and branch already exist, reuse succeeds
  only when the target is the expected clean linked worktree registered by the requesting repository, its absolute Git
  common directory exactly matches that repository, its branch and recorded green SHA match, and its HEAD is at or
  descends from the requested starting point. A same-named worktree from another clone is rejected without being
  adopted. The exact launch baseline remains recorded in linked-worktree Git metadata; one managed writer owns each
  linked worktree. Launch refuses a dirty worktree or a HEAD that is not at or descended from the recorded green
  baseline. The helper's optional `--resume-exact` mode is a preservation check for an already registered writer: its
  HEAD must equal the requested checkpoint, the recorded green must be that checkpoint or its ancestor, and it never
  moves HEAD, edits files, or rewrites green metadata. This mode itself grants no launch authority.
- Setup once: run `install -d -m 700 ~/.config/switchstand` and
  `install -m 600 switchstand-config.example ~/.config/switchstand/.env`, then fill in `ASANA_TOKEN`. This file is stable machine
  configuration; never put per-run work authority in it.
- Normal task-bound development: run `scripts/switchstand --active <Asana task URL or ID>`.
  It creates or resumes the task's private durable writer with bound `work_get` and ordinary development access.
  Before Codex starts, `scripts/bootstrap --verify-only` checks the primary's existing environment receipt;
  missing or stale setup requires `scripts/bootstrap` in the primary checkout and a relaunch.
  The launcher binds only the canonical `.venv` path and dependency-manifest digest for `scripts/check`, which
  verifies candidate manifests and reuses that environment without bootstrap or Docker. This editable development
  convenience is not immutable qualification evidence; exact-head CI and real canaries remain required.
  A launcher supervisor now retains ownership of a fresh child process group. On
  child exit or supervisor HUP/INT/QUIT/TERM, it sends TERM, waits one second,
  escalates to KILL if needed, and verifies no live group members remain before
  removing that invocation's `TMPDIR`. It holds the child PID until group cleanup
  completes and restores terminal foreground ownership. Other process groups,
  durable writers and persistent Codex/task state are retained. An unverifiable
  cleanup fails visibly and retains scratch files. Startup does not adopt or kill
  older sessions; SIGKILL of the supervisor and children that leave the owned
  process group are outside this bounded guarantee.
  The canonical task-private clone is resumed automatically. Dirty progress survives;
  normal Git, network, tests, review and landing remain available; MCP commands and hooks come from clean CONTROL.
- Managed Codex: run `scripts/switchstand-launch --active <Asana task URL> --commit <exact-candidate-SHA> --reference <reference URL>`. Task IDs work
  too, and up to eight `--reference` arguments are accepted. The launcher binds those human-readable tasks, injects
  their opaque handles for this process only, verifies the bounded `switchstand-development` profile and loaded
  instruction sources, then replaces itself with Codex. It refuses the ordinary checkout. Do not copy WorkIds or edit
  the shared environment file. Managed Codex runs have network access. At launch it pins a development image and
  starts a writer-local test network with egress for dependency resolution;
  the agent receives exact `quality`, `commit_all_current_worktree`, and read-only `run_status` tools. Ordinary commands cannot reach the
  Docker socket or shared Git metadata, and the quality tool never evaluates worktree-edited Docker instructions.
  During implementation, `scripts/check <affected-test-paths>` runs Ruff, strict Pyright, and affected tests from the stable host
  environment. One shared 120-second deadline starts before bootstrap/setup; bootstrap, manifest verification and each
  focused command consume only the remaining budget. Timed commands reserve a one-second forced-kill phase so a
  TERM-resistant setup cannot continue indefinitely after the deadline. Missing focused/setup prerequisites fail with
  an explicit rerun/setup action, and the focused path does not fall back to a full container build. CI supplies the
  routine full clean/container gate; real-host launch and recovery changes still require a real-host canary.
  Selecting a database-backed test without a live `TEST_DATABASE_URL` fails rather than skips.
  Focused and full-quality workload output has a 12,000-byte hard limit: output at or below the limit is accepted;
  exceeding it stops and removes the workload and returns a failed result. Reduce test or tool output, then rerun.
  Each launch writes a protected per-worktree run receipt with a fresh identity and Linux PID start token. The
  read-only `run_status` tool reports `running`, `stopped`, `lost`, or `unknown` without accepting an arbitrary PID.
  From that linked worktree, `scripts/switchstand-run-stop` uses the repository's bootstrapped Python environment and
  stops only the receipt's exact process identity: pidfd-pinned `SIGTERM`, a fixed bounded wait, then pidfd-pinned
  `SIGKILL`. It returns `lost` or `unknown` without signalling when identity cannot be proven, and never accepts a PID,
  signal, timeout, path, or process group.
- Isolated exact-candidate qualification: from the clean ordinary `main` checkout, run
  `scripts/switchstand --isolated --active <Asana task URL> --commit <exact-candidate-SHA>`. It freshly fetches `origin/main`,
  fast-forwards a clean ancestor `main` to that exact revision, and reads back a clean HEAD. Dirty or divergent main
  fails with local work intact. The trusted resolver reads only `ASANA_TOKEN` from the protected host config;
  the token is not exported into the candidate launch environment. The task must still contain exact base and candidate
  refs and SHAs matching the remote and the requested commit. A stale task binding stops launch after any safe main
  fast-forward. The launcher creates
  or reuses the task-named linked writer without moving or cleaning an existing worktree, then proves the candidate is
  registered to the same repository and at the requested commit. A dirty writer at that exact remote-bound checkpoint
  can resume with edits intact. New task writers live under the host's private 0700
  `~/.local/state/switchstand/worktrees` directory. A same-task legacy writer under `/tmp` or the caller's `TMPDIR`
  blocks creation of a second writer and remains untouched; reconcile it explicitly before relaunch. A locally moved
  HEAD, wrong task branch, foreign worktree, running/UNKNOWN prior run, or stale task binding fails with work intact.
  The accepted control-side launcher repeats
  the fetch, control/candidate/provenance checks immediately before managed effects and reports the observed revision.
  Task IDs, references, and one optional prompt are forwarded unchanged.
  The old `scripts/switchstand-context` and `scripts/switchstand-start` names are compatibility wrappers that warn.
- MCP server: `docker compose run --rm -T controller`
- Codex: the checked-in project MCP configuration launches the same required STDIO server. It forwards only `HOME`
  and the launch-scoped opaque authority; Compose obtains the provider credential from the protected shared file.
  The command sandbox cannot read that file. Direct `codex` invocation is not the supported managed path.
  A fresh head calls `work_get` with only `api_version="1"`; the tool defaults to its active assignment and advertises
  the opaque IDs of its bounded read-only references.

CI runs on Python 3.14 with PostgreSQL. Correctness, types, and tests block; formatting is reported without rewriting
review diffs. Stage branches and pull requests are based on the exact last accepted green SHA. Landing reconciliation
models the repository's squash flow: the landed result must be the current accepted result with the reviewed base as
its only parent, the reviewed candidate must descend from that base, and the landed tree must equal the reviewed
candidate tree. Integration admits State, Provider, then MCP and reruns affected plus full gates after each admission.

## Known recovery limits

The Stage-1 repairs above are bounded and do not close all code-red findings:

- **Docker ownership/cancellation:** current cleanup and timed Docker paths do not yet prove exact ownership and exact
  cancellation of every affected resource. Do not treat name matching or a timeout alone as ownership proof.
- **Independent CONTROL:** the current launcher/control path still receives launcher/Python source, Codex configuration
  and working-directory inputs from this repository, with the Codex binary selected from the ambient host path. It is
  therefore not the independently pinned CONTROL release required by the recovery design.

Canonical cumulative handwritten Python LOC is counted with
`git ls-files src tests | rg '\.py$' | xargs wc -l`; generated files and dependencies are excluded.
