# Development

Governed implementation tasks carry the inline package projection and review handoff
defined in [Code quality: governed implementation packages](code-quality.md#governed-implementation-packages).
Keep its package base fixed across delivery branches and use its solution disposition
before revising PR decomposition; routine implementation remains worker-owned.

## Normal local workflow

Prepare the shared tools once from the primary checkout, create a task-owned linked
writer at the exact accepted base, and run focused checks from that writer:

```bash
scripts/bootstrap
scripts/switchstand-worktree <writer-name> <exact-40-character-green-SHA>
cd ~/.local/state/switchstand/worktrees/<writer-name>
sh scripts/check tests/test_example.py -k relevant_case
```

`sh scripts/check <pytest args>` always runs Ruff and Pyright, then passes the supplied
arguments to pytest. Use `sh scripts/check tests/test_example.py` for one test module or
`sh scripts/check tests/test_example.py -k relevant_case` for one behavior. With no
pytest arguments it runs the full pytest suite. The command prepares or verifies the
locked environment automatically. In a linked writer it syncs that writer's exact
locked manifests into its own `.venv`, independently of later primary-checkout changes,
while reusing the pinned `uv` and download cache from the Git common directory. When
`TEST_DATABASE_URL` is absent, it also creates
the repository's owned disposable PostgreSQL instance and runs migrations before the
checks. An explicitly supplied URL bypasses automatic provisioning and must itself
identify an isolated `switchstand_test` database. The command does not make a dirty
writer an immutable candidate or replace the clean CI subject required for review.

For managed task-bound work, run the following command instead; it creates or resumes
the private writer and supplies the pinned development tools:

```bash
scripts/switchstand --active <Asana task URL or ID> -- <exact initial assignment>
```

For an ordinary Claude Code Coordinator, install the host shim once with
`scripts/install-claude-shim`, as for `codex`; raw `claude` then delegates to
`scripts/claude-dispatch` inside the canonical repository and runs plain Claude Code elsewhere. The Coordinator loads only
`.claude/coordinator-settings.json` and `.claude/coordinator-mcp.json`, excludes the global
`~/.claude/CLAUDE.md`, exposes the `switchstand` MCP, and fences the shared primary checkout with
`scripts/codex-hook --coordinator-primary` on Bash, Edit, MultiEdit, Write and NotebookEdit. It uses your
normal Claude login. Tool access grants no authority.

The detailed lifecycle, recovery, and isolated-candidate routes remain below.

## Match evidence to the claim

Record the immutable subject that each result exercised. A green result establishes
only that subject and boundary:

- **Exact head:** a named commit checked out and tested directly. This proves the
  checks that actually ran against that commit. It does not prove a pull request's
  composed result, an uncommitted worktree, or live activation.
- **Pull request composition:** the candidate combined with the current base. The
  Quality workflow records the candidate, base, and synthetic composition SHAs and
  verifies the composition's ordered parents. This proves the tested integration of
  those exact revisions; it is separate from exact-head evidence and can become stale
  when the base changes.
- **Integration or hermetic boundary:** a test that exercises real cooperating
  components inside a controlled boundary, such as real subprocesses, sockets, and a
  disposable PostgreSQL cluster. State the synthetic substitutions and the boundary
  left unproved. See [Disposable qualification](disposable-qualification.md) for the
  current process-level database path.
- **Activation, reliance, or live host:** evidence from the installed and configured
  runtime on which operators or clients will rely. Repository landing, unit tests,
  composition CI, and hermetic qualification do not establish this claim. Use the
  applicable qualification contract, such as the
  [ChatGPT MCP landing and activation split](chatgpt-mcp-edge.md#landing-and-activation-evidence)
  or [standalone Asana test project](standalone-test-project.md), and record the exact
  host/runtime, configuration, identity, and live action that were actually read back.
- **`NOT_RUN`, `MISSING_CAPABILITY`, `SKIP`, or `UNKNOWN`:** the named claim remains
  unproved. These states are not a candidate failure unless execution found an actual
  failure, and they are never a PASS. Preserve the state and the missing prerequisite
  instead of treating an aggregate green result as evidence that the boundary ran.

The governing distinctions and review rules live in
[Code quality: test quality and qualification](code-quality.md#test-quality-and-qualification).
Link to the applicable qualification document rather than copying its procedure into
a task or review handoff.

- Bootstrap/build: install Docker with Compose, then `docker compose build controller`.
- Stable host tools: run `scripts/bootstrap`. The primary checkout uses the bootstrap-owned primary `.venv`; each
  linked writer uses its own `.venv`. They share only the pinned `uv` binary and its Git common-directory cache and
  Python-install directories. Bootstrap obtains the pinned binary through Docker only when needed.
- Full clean/container quality runs in CI. Run `docker compose run --build --rm quality` locally only when changing
  Docker, runtime, or development-tool behavior that CI cannot qualify for the host.
- `scripts/local-quality` runs the exact CI Quality commands for a clean linked-writer head against an owned,
  disposable PostgreSQL 18 container. It builds the development image, mounts the current writer read-only (so the
  image's source tree cannot substitute for the candidate), shares only the database container's network namespace,
  and prints the candidate, image and container identities plus explicit PASS/FAIL/NOT_RUN and cleanup results. Before
  PASS it reverifies the exact head and clean tree; cleanup removes the owned containers and deterministic image tag.
  It refuses occupied deterministic container names and creates no Docker network. This is local exact-head evidence,
  not PR-composition, Docker-lifecycle, or live-activation evidence.
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
disposable PostgreSQL instance; and only then upgrades shared state. `rehearse`
proves an upgrade/downgrade/re-upgrade cycle without touching shared state;
`apply` upgrades shared state but
never activates database authority. Both create a private, fsynced JSON-lines
receipt bound to exact source revision `0007`, runtime SHA, Alembic head, dump,
counts, and deterministic schema/data digests. An `apply` attempt is durable
before mutation and the same receipt reconciles to `APPLIED`, `ABORTED`, or
`UNKNOWN`. Keep the reported dump and receipt until the upgraded
service has been exercised successfully.

Every invocation names its target explicitly. `--target production` binds the
canonical Compose project, volume, network, and receipt directory. A copied-state
qualification instead uses `--target disposable:NAME`, which derives a distinct
`switchstand-rehearsal-NAME` Compose namespace under the pre-created, private,
non-symlink `~/.local/state/switchstand/rehearsals/NAME` root. Receipts bind that
exact target, project, volume, and network, so resume or abort cannot cross targets.
Selecting a target does not create or populate it.

`switchstand-rehearsal-target provision NAME` is the separate copied-state
provisioner. It creates only the canonical private rehearsal root, records a
mode-`0600` descriptor binding the exact candidate, source database backup and
FastMCP snapshot digests, namespaced Compose resources, copied-state paths, and
three isolated loopback endpoints, then restores the backup into its disposable
PostgreSQL service and extracts the bounded FastMCP tree. Before Compose starts, it
creates that namespace's default network on the deterministic `/24` selected by the
canonical development-subnet policy; the descriptor, provisioning readback, READY
validation, and teardown bind both its exact Compose labels and IPAM subnet.
`teardown NAME` first revalidates that descriptor and every Compose label, then
removes only its container, volume, network, and private root. A failed provision durably records
its exact step, exit status, and bounded diagnostic; teardown also accepts that
exact `FAILED` descriptor and removes only the zero-or-one resources whose namespace,
identity, and labels it proves. A production, incomplete, or foreign identity
is rejected before removal. Provisioning and teardown do not change production
services, state, settings, or routing.

`stage12_cutover.ConcreteCommands` binds the offline state machine to reviewed migration
commands. Disposable runs derive `DATABASE_URL` from their namespaced PostgreSQL container.
A later host CLI must reconcile process loss; this adapter grants no activation authority.

Before either Stage 1 or Stage 2 `POSTGRES_AUTHORITY` marker exists,
`abort-pre-authority <apply-receipt>` may perform the exact receipt-bound
downgrade and verify the original revision and digests. Either marker, unreadable
marker state, an ambiguous migration outcome, or any digest mismatch forbids
rollback and retains the maintenance gate for forward repair. The command never
automatically restores a backup.

Stage 1 `prepare` requires a create-new receipt and durably records the exact
before/final/inserted Asana handle bindings before commit. Use `prepare-reconcile`
after ambiguous output; `prepare-cleanup` removes only that exact inserted set while
both authority markers are absent and the full binding map still matches. `UNKNOWN`
keeps maintenance active and forbids schema rollback or old-runtime restart.
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
  `scripts/check` runs locked sync into the writer's `.venv`; `uv` reuses the Git common-directory cache and existing
  writer environment on re-entry. The writer does not use the primary `.venv` or its freshness receipt for checks.
  This development convenience is not immutable qualification evidence; exact-head CI and real canaries remain required.
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
  - the development MCP in `development.py` is a separate local development boundary for selected-test
    `check`, commit, non-authoritative `diagnostic_full_suite`, and run status. The diagnostic runs the
    full suite inside the deliberately constrained managed sandbox; its result does not establish CI
    equivalence or full qualification. Exact-head/composition GitHub Quality remains authoritative.
    This surface does not grant product work authority.
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
