# Development

Governed implementation tasks carry the inline package projection and review handoff
defined in [Code quality: governed implementation packages](code-quality.md#governed-implementation-packages).
Keep its package base fixed across delivery branches and use its solution disposition
before revising PR decomposition; routine implementation remains worker-owned.

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
- Normal task-bound development: run
  `scripts/switchstand --active <Asana task URL or ID> -- <exact initial assignment>`.
  It creates or resumes the task's private durable writer with ordinary development access. Its single initial request
  requires the agent to read bound `work_get`, then page bound `work_history` at that returned revision before material
  work. A stale history read restarts from a fresh `work_get`, so later completion or supersession evidence is
  reconciled without exposing arbitrary source-task reads. Both context tools are read-only and approval-free; the
  active WorkId is launcher-bound and is not a tool argument.
  CONTROL supplies its existing pinned `uv` using `scripts/bootstrap --print-uv`.
  `scripts/check` runs locked sync into the writer's `.venv`; `uv` reuses its normal local cache and existing
  environment on re-entry. No primary `.venv` or separate freshness receipt is needed for checks. This development
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
- Managed exact-candidate Codex qualification uses the isolated CONTROL route below; there is no
  candidate-local trusted launcher. The candidate remains writable work input only, while CONTROL owns launcher Python,
  Codex project configuration and managed/development MCP wrappers. Live use remains fail-closed until the separately
  managed external selector is installed with an ACTIVE CONTROL manifest; repository landing does not install, activate
  or cut over that selector.
- Isolated exact-candidate qualification: from the clean ordinary `main` checkout, run
  `scripts/switchstand --isolated --active <Asana task URL> --commit <exact-candidate-SHA>`. This command delegates
  only to the fixed user-level external selector at `$HOME/.local/bin/switchstand-start`; there is no repository or
  candidate fallback. Until that separately managed selector has an ACTIVE manifest/CONTROL readback, the command
  intentionally fails closed and is not reliance-ready. Once active, the selected CONTROL-internal launcher freshly
  fetches `origin/main`,
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
  The selector's SHA-keyed CONTROL snapshot owns launcher Python, Codex project configuration and managed/development MCP wrapper code; the candidate is only an explicit linked-worktree/add-dir input. Qualification includes hostile candidate shadow code/config and must prove it cannot substitute those CONTROL implementations.
  Task IDs, references, and one optional prompt are forwarded unchanged.
  `launch_source.py` resolves the protected task-source contract, while `candidate.py` verifies/materializes the
  exact remote base/candidate refs used by isolated launch before `launch.py` performs its final provenance preflight.
  The old `scripts/switchstand-context` and `scripts/switchstand-start` names are compatibility wrappers that warn.
- Product MCP surfaces are intentionally distinct:
  - authenticated ChatGPT uses the HTTP/OAuth edge in `chatgpt_edge.py` with workspace grants and explicit WorkIds;
  - managed task-bound Codex uses the STDIO server from `mcp.py`, with active/reference WorkIds injected only by the
    trusted launcher;
  - the development MCP in `development.py` is a separate local development boundary for check/commit/quality/run
    status and does not grant product work authority.
  Raw `source_task/source_stories/source_story` remain transitional compatibility reads; prefer WorkId-based APIs for
  new ordinary flows. Provider credentials remain in trusted host/service configuration and are not agent arguments.

CI runs on Python 3.14 with PostgreSQL. Correctness, types, and tests block; formatting is reported without rewriting
review diffs. The development/Quality image must support the production Git command contract, including
`--no-lazy-fetch`; landing-reconciliation tests fail with `MISSING_CAPABILITY` rather than skip if that prerequisite
regresses. Pytest short summaries expose any remaining SKIPPED/XFAIL claims so aggregate Quality SUCCESS does not
silently imply they ran. Stage branches and pull requests are based on the exact last accepted green SHA. Landing reconciliation
models the repository's GitHub merge-commit flow: the landed result must be the current accepted result with exactly
the reviewed base and reviewed candidate as its ordered parents, the reviewed candidate must descend from that base,
and the landed tree must equal the reviewed candidate tree. Integration admits State, Provider, then MCP and reruns affected plus full gates after each admission.

## Known recovery limits

The current development path is operational but still has explicit ownership/transition debt:

- **Launch/Docker ownership:** `development.py` now owns candidate development-resource preparation/cleanup and
  workload mechanics, while `docker.py` owns shared exact-object primitives. `launch.py` still owns orchestration
  decisions about when those lifecycle operations occur. Treat that remaining policy split as cleanup debt, not as
  duplicate low-level Docker ownership.
- **Launch-source ownership:** isolated launch still uses `launch_source.py` for protected task-source parsing/readback;
  exact Git remote/ref verification/materialization is owned by `candidate.py`. Do not copy the transitional source
  bridge into new product code.
- **Legacy source MCP:** raw `source_*` tools remain exposed for recovery/reference compatibility until neutral
  replacement coverage and legacy-drain proof exist.
- **Independent CONTROL reliance:** repository trust convergence routes isolated launch only through the external
  SHA-keyed selector and removes the candidate-local trusted launcher. Live ordinary-use reliance remains separately
  blocked until the installed selector path and ACTIVE CONTROL manifest are authorized and read back; no repository
  fallback is permitted during that gap.

Canonical cumulative handwritten Python LOC is counted with
`git ls-files src tests | rg '\.py$' | xargs wc -l`; generated files and dependencies are excluded.
