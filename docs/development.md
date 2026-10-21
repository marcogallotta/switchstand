# Development

The retired custom Codex launcher and its private Wakeful adapter are not development paths.
Ordinary Codex uses the official CLI and repository configuration. Neutral Wakeful persistence,
edge monitoring, and the optional disk-pressure producer remain independent components; installing
the disk-pressure timer is still a separate activation effect.

Governed implementation tasks carry the inline package projection and review handoff
defined in [Code quality: governed implementation packages](code-quality.md#governed-implementation-packages).
Keep its package base fixed across delivery branches and use its solution disposition
before revising PR decomposition; routine implementation remains worker-owned.

## Implementation through preactivation

An in-scope implementation dispatch authorizes all benign work through preactivation readiness: commit/PR, independent review, tests/CI, corrections, integration and nonproduction landing, plus nonproduction activation, failure and rollback rehearsals with tested rollback ready. Do not stop at pre-merge, landing or another artificial phase boundary. Preserve current specific `HOLD`/`STOP`/`DENIED`, required independent review, exact-candidate/composition and technical gates; a blocked effect does not abandon other readiness work.

Anything that actually changes production waits for Marco’s exact Go for that effect and candidate, including a merge that automatically deploys or changes live reliance. Implementation dispatch does not grant production, migration, credential or protected provider effects. Record any production-impacting landing block and finish all remaining benign preparation.

Before claiming ready, prepare dispatch and rehearse the complete supported activation → failure handling → rollback path on TEST or a disposable equivalent. Measure Go through dispatch, execution, required currentness checks and authoritative readback; report elapsed time and limitations. For Marco’s current five-second bound, readiness requires that complete relevant path demonstrated within five seconds. Patch/rollback microbenchmarks alone are insufficient; do not promise unmeasured network or model response times.

## Normal local workflow

Prepare the shared tools once from the primary checkout, create a task-owned linked
writer at the exact accepted base, and run focused checks from that writer:

```bash
scripts/bootstrap
scripts/switchstand-worktree <writer-name> <exact-40-character-green-SHA>
cd ~/.local/state/switchstand/worktrees/<writer-name>
sh scripts/check tests/test_example.py -k relevant_case
```

For an ordinary fresh copy of the latest `main`, run `scripts/new-worktree <name>` and change to the printed path. The helper fetches `origin/main`, creates the copy under `~/.local/state/switchstand/worktrees` on branch `v2-<name>`, and refuses an existing name.

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
scripts/switchstand --active <WorkId or legacy task URL/ID> -- <exact initial assignment>
```

For an ordinary Claude Code Coordinator, install its separate host shim once with
`scripts/install-claude-shim`; raw `claude` then delegates to
`scripts/claude-dispatch` inside the canonical repository and runs plain Claude Code elsewhere. The Coordinator loads only
`.claude/coordinator-settings.json` and `.claude/coordinator-mcp.json`, excludes the global
`~/.claude/CLAUDE.md`, exposes the `switchstand` MCP, and fences the shared primary checkout with
`scripts/codex-hook --coordinator-primary` on Bash, Edit, MultiEdit, Write and NotebookEdit. It uses your
normal Claude login. Tool access grants no authority.

The ordinary Codex Coordinator profile runs directly operated Root and sub-Coordinators with native
`danger-full-access` and `approval_policy=never`, so host user-session IPC, services, files, and
networking are available without a second approval path. Repository policy still forbids canonical
primary source mutation. The active repository Bash hook blocks recognized source-changing Git
commands only when Git proves their
target is the canonical primary checkout; routine push, upstream and repository-metadata operations
remain available, and unavailable target identity fails open with an advisory. It creates no
separate state or service: Codex's `/hooks` browser is the direct emergency
off-switch for this individual non-managed project hook and does not traverse the guarded Bash path.
It is a guardrail, not complete shell or operating-system isolation. Arbitrary local-user shell code
remains technically capable of writing the canonical checkout, so implementation must stay in an
owned writer. Unbound Workers stay read-only; bound Workers get writer-confined edits while it blocks
auto-memory writes. Full local-user execution is not provider, deployment, activation, or other
consequential-effect authority; those exact grants and readbacks remain separate. Malformed auxiliary
friction state is quarantined and regenerated visibly so launch and unrelated diagnosis remain usable.

The detailed lifecycle, recovery, and isolated-candidate routes remain below.

For real ChatGPT testing of an unreleased candidate, start with the
[isolated TEST MCP lifecycle](chatgpt-mcp-edge.md#isolated-real-chatgpt-test-mcp).
Check the existing TEST service and its current owner before creating another;
use its separate connection and database for candidate qualification.

## Noninteractive commands from an Implementation Specification

Specification authors should include every required noninteractive command in one
`spec-test-commands` JSON fence in the Implementation Specification's notes. Each head runs
these authorized local checks directly:

````text
```spec-test-commands
[{"name":"focused quality","argv":["sh","scripts/check","tests/test_example.py"],
  "cwd":".","timeout_seconds":120,"noninteractive":true}]
```
````

When resuming an older specification without the block, add the agreed executable checks
before using the runner, preserving its acceptance criteria; no bulk rewrite is needed.
Keep visible or human ChatGPT testing duties separately identified and Coordinator-owned.

The executing head reads current work through `work_get`, saves its successful JSON payload
(or full CallToolResult) outside the candidate, and runs against that exact specification
identity and clean candidate:

```bash
scripts/spec-test-commands --spec /tmp/spec.json --work-id <WorkId> \
  --revision <exact-revision> --candidate <40-character-SHA> --repo "$PWD" > /tmp/results.json
```

The runner reads the specification; there is no caller-maintained command subset. Every entry is
required. Only all actual zero exits plus unchanged source bytes and clean candidate produce PASS.
Missing/invalid commands or prerequisites remain NOT_RUN; failures/timeouts are FAIL; detected
post-run source/candidate drift is STALE. The JSON records each argv, cwd, output, exit and duration
with the specification identity/digest, candidate, reason, next owner and action. Closed stdin,
CI/display guards and process-group timeouts support noninteractive execution. These guards are
not a sandbox: the head must select only authorized noninteractive checks that do not launch a
browser or affect live services. An export cannot prove live canonical currentness; refresh the
exact spec before execution and reconcile before recording its result.

The executing head records the exact result once through existing Tracker `work_update`; the
runner prints evidence and does not write Tracker or send a PASS mailbox message. On partial or
failed evidence, diagnose why and choose code/fixture/spec repair, genuine human input, or a real
blocker; retain reason/owner/next action. Independent review challenges that judgment, including
when no change is chosen. Local execution needs no Coordinator approval. Existing review and
qualification requirements still apply to their own boundaries.

## Match evidence to the claim

Record the immutable subject that each result exercised. A green result establishes
only that subject and boundary:

- **Exact head:** a named commit checked out and tested directly. This proves the
  checks that actually ran against that commit. It does not prove a pull request's
  composed result, an uncommitted worktree, or live activation.
- **Pull request composition:** the candidate combined with the current base. The
  Quality workflow records the candidate, base, and synthetic composition SHAs and
  checks out the event's immutable SHA and verifies the composition's ordered parents.
  A regenerated PR merge reference cannot silently change the checkout. This proves
  the tested integration of those exact revisions; it is separate from exact-head evidence and can become stale
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

GitHub-native stacks may be authored, self-checked, and sent for focused layer review
asynchronously. Each layer remains a bounded semantic change. Fix a defect in the lowest
layer that owns it and cascade the restack upward. A restack preserves a focused review
only when both the layer-diff identity and the reviewed dependency/interface-contract
identity are unchanged and no conflict was resolved. Every included layer still waits for
fresh exact-top cumulative review and qualification before landing; partial early landing
is outside V1. Manual or unrecognized stacks keep the ordinary unstacked gates.

Quality also runs the same authoritative Quality and Docker lifecycle jobs daily against
the exact default-branch head supplied by GitHub. Scheduled runs are labeled separately
from push exact-head and pull-request composition runs. GitHub schedules are best-effort:
until a separate freshness monitor exists, this backstop is **SCHEDULED/BEST-EFFORT**, not
an operational guarantee. A real scheduled run is required before claiming it is live.

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
  That cleanup then runs `scripts/docker-gc`, which serializes host-wide cache reconciliation and caps unused BuildKit
  cache at 20 GB. Managed agent startup/re-entry and termination use the same progress-triggered GC after exact owned
  resource cleanup. GC never removes images, containers, networks, or volumes; a GC failure is a visible cleanup
  failure rather than a silent residue leak.
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
disposable PostgreSQL instance; and only then upgrades shared state. Invoke it as
`scripts/switchstand-upgrade-state --target production`. It does not activate a
runtime or authority switch. Keep the reported backup until the upgraded service
has been exercised successfully; a failed or ambiguous shared migration is not
retried or rolled back automatically.

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
  `install -m 600 switchstand-config.example ~/.config/switchstand/.env`. This file is stable machine
  configuration; the current managed runtime needs no provider credential, and per-run work authority never belongs in it.
- Normal task-bound development: run
  `scripts/switchstand --active <WorkId or legacy task URL/ID> -- <exact initial assignment>`.
  It creates or resumes the task's private durable writer with ordinary development access. Its single initial request
  requires the agent to ground from bound `work_get` and the current durable package before material work. Normal
  startup, re-entry, review, polling, and implementation do not enumerate `work_history`. History remains a read-only
  exceptional surface for explicit bounded investigation or recovery; any controlling conclusion from that exceptional
  read must be reconciled back into current durable work before ordinary execution continues. Both context tools are
  read-only and approval-free; the active WorkId is launcher-bound and is not a tool argument.
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
- Cross-repository ordinary prototype (ai-tools only):
  `scripts/switchstand --active <task> --target-repo <absolute canonical ai-tools checkout> -- <assignment>`.
  Trusted CONTROL remains Switchstand. Before any target writer effect, provisioning binds the WorkId
  and reads current work through `Controller.get`; notes must contain exactly one
  `SWITCHSTAND_REPOSITORY=marcogallotta/ai-tools` marker. No candidate/base markers are required.
  TARGET is an identity anchor and may be dirty; its exact GitHub origin must match admission.
  WRITER is an independent repo-scoped clone from freshly fetched target `origin/main`, never
  CONTROL or dirty target contents. Repo-bound writer and CODEX_HOME identities reject same-task
  cross-target/mode reuse. Dirty files and local commits resume exactly; only an untouched writer
  follows newer target main. Omitting `--target-repo` preserves legacy paths and behavior.
  Dish owns its native environment: `cd dish`, `python3 -m venv --clear .venv`, then
  `.venv/bin/python -m pip install -r requirements-test.txt` and task-relevant focused pytest.
  Switchstand's pinned uv remains launcher-only. Credential stripping and the launch-bound profile
  of the canonical CONTROL MCP remain active. Publication and live Dish proof are unproved here;
  the latter requires the separately
  authorized ai-tools bootstrap bridge. This prototype does not generalize isolated qualification.
- Managed exact-candidate Codex qualification uses the isolated CONTROL route below; there is no
  candidate-local trusted launcher. The candidate remains writable work input only, while CONTROL owns launcher Python,
  Codex project configuration and managed/development MCP wrappers. Live use remains fail-closed until the separately
  managed external selector is installed with an ACTIVE CONTROL manifest; repository landing does not install, activate
  or cut over that selector.
- An operator may stage and select an exact current-main CONTROL with
  `python -m switchstand.control_release <target-sha>`. It validates through the installed selector without executing
  the candidate, and fails before staging unless that selector exactly matches this release; the direct four-field
  manifest method remains the fallback and rollback path.
- Isolated exact-candidate qualification: from the clean ordinary `main` checkout, run
  `scripts/switchstand --isolated --active <WorkId or legacy task URL/ID> --commit <exact-candidate-SHA>`. This command delegates
  only to the fixed user-level external selector at `$HOME/.local/bin/switchstand-start`; there is no repository or
  candidate fallback. Until that separately managed selector has an ACTIVE manifest/CONTROL readback, the command
  intentionally fails closed and is not reliance-ready. Once active, the selected CONTROL-internal launcher freshly
  fetches `origin/main`,
  fast-forwards a clean ancestor `main` to that exact revision, and reads back a clean HEAD. Dirty or divergent main
  fails with local work intact. The trusted resolver starts the canonical state database, requires exactly one
  reachable container-network address, and resolves WorkId, legacy aliases, and launch notes from PostgreSQL.
  It does not read or export `ASANA_TOKEN`. The task must still contain exact base and candidate
  refs and SHAs matching the remote and the requested commit. A stale task binding stops launch after any safe main
  fast-forward. The launcher creates
  or reuses the task-named linked writer without moving or cleaning an existing worktree, then proves the candidate is
  registered to the same repository and at the requested commit. A dirty writer at that exact remote-bound checkpoint
  can resume with edits intact. New task writers live under the host's private 0700
  `~/.local/state/switchstand/worktrees` directory. Any canonical legacy-GID writer in that durable root, `/tmp`, or
  the caller's `TMPDIR` blocks creation of a second writer regardless of input spelling and remains untouched;
  reconcile it explicitly before relaunch. A locally moved
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
  Provider source and attachment tools are retired from managed and ordinary MCP surfaces. Legacy
  task references resolve locally to WorkIds.

CI runs on Python 3.14 with PostgreSQL. Correctness, types, and tests block; formatting is reported without rewriting
review diffs. The development/Quality image must support the production Git command contract, including
`--no-lazy-fetch`; landing-reconciliation tests fail with `MISSING_CAPABILITY` rather than skip if that prerequisite
regresses. Pytest short summaries expose any remaining SKIPPED/XFAIL claims so aggregate Quality SUCCESS does not
silently imply they ran. Stage branches and pull requests are based on the exact last accepted green SHA. Landing reconciliation
models the repository's GitHub merge-commit flow. For a direct or rebased candidate, the landed result has the exact current
reviewed base and candidate as ordered parents, the candidate descends from that base, and the landed tree equals the candidate
tree. When an immutable candidate is preserved after target advancement, it keeps its original reviewed-base ancestry; the landed
result instead has the exact current base and immutable candidate as ordered parents, and its tree equals the exact qualified
composition tree. Integration admits State, Provider, then MCP and reruns affected plus full gates after each admission.

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

Official Codex installation and updates are owned by npm. The repository does not install,
repair, wrap, or redirect the global `codex` command.

Quality emits non-authoritative test timing in `test-metrics-<run_id>-<run_attempt>-quality-shard-<index>`
artifacts retained for 90 days, plus a bounded slow-test job summary. The original
Quality exit status remains authoritative even when collection/upload fails. JUnit and
JSON preserve failed/partial attempts separately from reruns. JSON binds exact subject,
GitHub run/attempt/job and environment identities (workflow, dependency/config blobs,
Python and runner image, plus actual sorted distro/Python package versions and
Python/uv binary hashes from the built container); the source-bearing candidate image is recorded separately
so ordinary source changes do not invalidate comparisons. Unknown environment fields yield a null
comparison key; mismatched/unknown environments require INSUFFICIENT_DATA in future
trend analysis. GitHub job metadata supplies CI duration; JUnit supplies suite/test
time. `PYTHONPATH=src python3 -m switchstand.test_metrics --help` describes the shared formatter;
optional planner selections count files, never JUnit cases. The authoritative foreground Quality
job promotes direct test-only changes as `PROMOTE_TEST_MODULE_ONLY_V1`. It also promotes
`PROMOTE_AFFECTED_INPUTS_V1` for `scripts/package-size.py` (explicit subprocess consumer
`tests/test_package_size.py`) and `src/switchstand/coordinator_worker_state.py` (all transitive
Python test consumers), optionally alongside direct `tests/test_*.py` changes. Every changed input
must be accounted for; unknown inputs and missing explicit consumers force full fallback.
PR selection uses the verified execution composition, including consumers added by the current
base. Both promoted classes require exact current identity, direct selected test modules, no
delete/rename, planner fallback, serial-sensitive test, selector-health warning, or cumulative
native-stack-top obligation. Other production/helper/configuration/documentation/workflow/migration
changes remain full. Default-branch pushes and the nightly schedule always run the full suite; a
missing or unsuccessful current default-branch Quality result, or any failed default/scheduled broad
result in the same workflow generation, demotes both promoted classes to full fallback. The
authoritative planner uses Python 3.14. Once the exact validated plan and mandatory gates make
promotion impossible, history and adaptive observations are skipped and explicitly `UNKNOWN`;
eligible candidates still require the complete same-generation health check. Unresolved
run identity or a provider result cap also fails closed. A later green run cannot clear that
generation's demotion; correction changes the reviewed workflow generation. History acquisition
reads counts and runs together, requesting another 100-run page only while the current generation
remains open. A known different workflow blob closes that event's generation; otherwise every
reported run below the provider's 1,000-result cap must be observed. Missing/changed counts,
duplicate observed run IDs, truncated pages and provider failures preserve UNKNOWN/full fallback.
An unresolved Git blob cannot establish a generation boundary. Docker lifecycle
evidence is omitted only for direct-test-only promotion; selected source/script execution still
requires successful Docker lifecycle evidence. One stable
terminal Quality check runs under `always()` and fails unless planning, Quality execution, and the
mode-required Docker result are all valid; internal mode never changes its required check name.
Full fallback runs four independent GitHub runners, each with its own PostgreSQL service.
`scripts/pytest-shard` balances whole modules from pytest's actual collection using checked-in
observed JUnit costs in `scripts/pytest-shard-weights.json`. Unmeasured modules use a fixed default;
module-path and shard-index ties make assignment independent of collection order. Every shard
gets a module when enough modules exist. The last shard's estimated load includes static checks.
These weights guide scheduling, never selection. Each shard retains full collected and assigned
node IDs plus module membership;
its `full-shard` timing record covers only that partition. Ruff/Pyright run once on the last shard,
including the sole runner in selected execution.
The promoted selected-module path runs once on shard zero. Matrix fail-fast is disabled, and
terminal success requires the entire matrix to succeed; failures, cancellations and skips fail closed.
Planner identity,
mode, selected membership, JUnit, and timings are retained with the authoritative result. There is
no telemetry service or second test scheduler.
Qualification-attempt lineage is advisory. A stable logical lineage contains provider-idempotent
GitHub run/attempt identities, exact subject/plan/harness/environment scope, typed retry reasons,
cumulative timing, and harness/wrapper/fixture/setup rework. Missing or conflicting currentness is
`UNKNOWN`; identical provider replays deduplicate and conflicting replays fail closed. A
`CI_QUALIFICATION_REWORK_WARNING` request is deterministic and once-per-lineage, but live delivery
remains dependent on the existing Tests & CI to Wakeful attention route. V1 retains each attempt as
a 90-day workflow artifact but does not fetch prior artifacts, classify failure/rework, summarize a
live lineage, or submit attention. The summarizer and warning-request projector are an inert library
foundation until that delivery dependency exists. They do not block retries or change qualification
or merge authority.
