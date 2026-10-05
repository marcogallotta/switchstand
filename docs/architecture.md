# Architecture

Switchstand is a bounded, provider-neutral work controller. Stable `WorkId` values are the
application identity for work; provider task IDs stay behind trusted state and provider adapters.
The repository has several runtime surfaces with deliberately different authority. A tool being
implemented in one surface does not make it available or authorized in another.

## Semantic owners and request flow

The ordinary authenticated flow is:

1. `chatgpt_edge.py` authenticates the HTTP caller, creates the runtime dependencies, and registers
   the canonical ordinary tools produced by `chatgpt_mcp.py::build_ordinary_tools`.
2. `chatgpt.py::ChatGPTService` resolves caller admission and routes ordinary work operations to
   the canonical PostgreSQL owners.
3. `canonical_work_runtime.py`, `effects.py::CanonicalAppendGateway`, and the canonical event and
   relation repositories validate currentness and apply work mutations.
4. `grant_state.py` journals each protected mutation in the same transaction as canonical state.

`contracts.py` owns shared closed request/result models. `core.py::Controller` is the bounded work
read/source controller used by managed launches and by the exact-source compatibility methods; it
does not own ordinary HTTP authentication or grant issuance.

## Runtime surfaces

### Authenticated ordinary HTTP MCP

`chatgpt_mcp.py::build_ordinary_tools` is the canonical definition of the ordinary tool inventory
and behavior. It includes provider-neutral work discovery, history and events; protected
append/create/update/relation operations; registered-name `agent_message_*` messaging;
required-result persistence; and repository bundle transport. Provider-era effects receive any
provider readback through the exact pre-cutover deployment while its provider is still configured.
Separately authorized frozen-cutover operator action archives or deletes the adjudicated rows before
the DB-native application is deployed. Managed task-bound runtimes separately retain the
WorkId-addressed `message_*` family.

`chatgpt_edge.py` is the HTTP/OAuth edge, not a second tool definition. Its
`oauth_continuity.py` provider restricts authentication to the configured GitHub user and keeps one
validated upstream GitHub credential behind a stable storage identity; downstream client JWTs,
JTIs, grants, and runtime identities remain distinct. Legacy JTI mappings converge lazily to that
credential. A still-valid signed downstream token can rebuild a missing or expired JTI mapping only
while its signed claims remain valid, its stored client metadata remains current for refresh, and
that canonical upstream credential still passes the configured identity and scope checks. Explicit
and transparent refresh share one process-local serialization boundary. A consumed downstream
refresh token has a process-local five-minute, 128-entry replay window that returns the exact
already-issued token response only to the same client and identical original and requested scopes;
it never repeats the upstream refresh. The replay cache is intentionally not restart-persistent, so
a duplicate arriving after an edge restart is rejected rather than reviving consumed token state.
The edge bridges the authenticated request into a `RequestPrincipal`, builds
the PostgreSQL-backed service, registers every ordinary tool with FastMCP, and supplies the
MCP session ID used for message-currentness fencing. Repository MCP configuration and deployment
configuration must expose the same intended inventory, but tool semantics belong in
`build_ordinary_tools`. The edge also owns the narrow HTTP lifecycle integration that completes a
standalone Streamable HTTP GET when the SSE dependency returns during shutdown.
`observability.py` owns request-local, redacted terminal timing records and active-request SQL
interval aggregation; it neither persists records nor establishes journal durability or reliance.
`flow_report.py` owns a private read-only JSON snapshot for one exact WorkId. B1 is always partial:
it reports canonical current state and relations plus observed target events from one read-only,
repeatable-read transaction. It names excluded sources and computes no elapsed time; journal timing
remains unavailable until cross-restart retention is proved. Its concise format is a deterministic
projection of the same JSON facts and coverage reasons, not another correlation or inference layer.
Optional GitHub evidence requires a caller-supplied pull-request number and expected exact head;
the existing repository-candidate qualifier validates their current identity. Exact-head and
composition gate intervals remain separate, and overlapping intervals are unioned within each
subject. Provider, identity, stale-candidate, or incomplete-timestamp uncertainty produces
`UNKNOWN` rather than an inferred span.
Direct `package_work_id` Human Review rows add prepared/decided point evidence and recorded review
waits. The report clips validated aware intervals to the admission-to-capture wall window and unions
overlaps; GitHub subjects feed that same union rather than being summed. The remaining wall time is
explicitly unobserved, not idle time or a critical path, and unsafe clocks or intervals fail closed.
When the inert Human Review store is not installed, the report keeps that source explicitly
`UNKNOWN / SOURCE_TABLE_UNAVAILABLE` and continues the same read-only snapshot without fabricating
review evidence.
Outcome-state evidence is target-only through exact `owner_work_id` equality and is validated with
the existing revision-chain oracle against the canonical row revision. Reports expose at most 64
privacy-safe revision headers and item-status counts; payload text is excluded, corrupt chains are
`UNKNOWN`, and revision timestamps remain zero-duration observational points.
Human-trajectory evidence likewise uses exact `work_id_ref` and the trajectory owner's complete
payload/digest-chain validator, but exposes only the latest 64 trajectory ID/generation/source-kind
timestamps. It labels a sound chain `VALIDATED` without claiming canonical currentness; sensitive
trajectory payload and provenance references never enter the report.

`stable_auth.py` is the inert single-host split-auth owner. It can build a stable application that
owns the existing `SwitchstandGitHubProvider`, public OAuth routes, encrypted FastMCP state, JTI
mappings, and a private authenticated per-request introspection route. Its edge verifier accepts
only the exact configured issuer, resource, immutable numeric GitHub subject, scope set, nonempty
client ID, and sufficiently future integer expiry. It owns a finite per-call deadline (two seconds
by default and never more than five) and streams at most 4 KiB of response data, closing on the
first excess byte; transport, authentication, status, size, and schema failures reject the bearer
token. `chatgpt_edge.py::create_delegated_app` can wire that
verifier into the unchanged ordinary tool surface without GitHub credentials, signing keys, or an
OAuth store. The ordinary `serve` entry point still calls the combined `create_app`; no split
process is activated by default. `stable_auth_runtime.py` adds explicit default-off stable-auth and
delegated-edge launch modes. The two processes share only a mode-0600 internal credential and an
authenticated loopback introspection call; separate environment files keep the GitHub secret and
FastMCP state configuration out of the delegated edge. `stable_auth_host.py` owns offline
credential provisioning/rotation/readback plus inert systemd and Caddy asset generation. Its Caddy
contract routes only public OAuth paths to the stable service and MCP/resource metadata to the
delegated edge; it has no public match for private introspection. The host layer does not install,
apply, start, migrate, or activate those assets.
`stable_auth_migration.py` owns offline exclusive-writer copy/checksum and immutable receipts for
backup, relocation, explicit restore, and preserve-current-state rollback evidence. It never
controls services or automatically restores OAuth state.
`stable_auth_deployment.py` owns the inert, first combined-to-split activation transaction. It
serializes with ordinary edge maintenance, retains a publicly verified Caddy gate across the
service/state/route transition, binds both split processes to one exact candidate source tree, and
transfers persistent systemd startup ownership with exact readback. Caddy changes are scoped to
the four owned proxy IDs so unrelated ingress changes are preserved. It automatically restores the
combined service only while public exposure is still disproved. Any
ambiguous or post-exposure outcome stays gated and never rewinds OAuth state. It does not own the
future split edge-only replacement transaction, candidate preparation, live qualification, or
activation authority.
`chatgpt_mcp.py::ordinary_tool_annotations` is the exhaustive owner for ordinary client-visible
metadata. This private single-user host intentionally advertises
`readOnlyHint=true` for every current ordinary tool as a ChatGPT approval-prompt workaround; it is
not a claim that durable operations have no state effects. Deterministic server-side admission,
authority, revision, identity, transition, and payload validation remain the safety boundary.
Ordinary creation is parent-WorkId-only, and ordinary relation changes accept only parent or
dependency WorkIds; raw project, section, and assignee identifiers stay behind trusted internal
and provider boundaries.
Every current tool is explicitly non-destructive and bounded rather than open-world. Current tools
are idempotent under their stable identity or transition contracts. Registration rejects a new
ordinary tool until that classification is extended.

`edge_maintenance.py` owns the service-specific maintenance-window transaction for replacing this
edge. It gates all public Switchstand MCP/OAuth routes in Caddy before shutdown, snapshots FastMCP
transport state only while the service is offline, and runs the existing shared-state upgrade only
after the gate is public and the systemd service is confirmed stopped. Its receipt persists an
`UPGRADE_PENDING` boundary before that forward-only operation and the irreversible upgraded-state
fact after success, so migration ambiguity remains gated rather than being retried or rolled back.
It swaps the launcher atomically, verifies locally
before ungating, and preserves `UNKNOWN` behind the gate. Its exact receipt runner consumes a
read-only host-phase reconciler that revalidates runtime and artifact trust boundaries and accepts
only the durable phase or one exactly proven next state. Its production-only no-effect recovery can
terminalize one exact `UNKNOWN / UPGRADE_PENDING` receipt only while both maintenance locks are
held and the old healthy runtime, unchanged receipt, exact pre-upgrade revision, and absent target
table all agree; it performs no retry or host/database mutation beyond the terminal receipt. It
does not own tool semantics, OAuth state format, host installation, or activation authority.

`wakeful.py` is an inert, agent-system-neutral event persistence prototype. It owns the sanitized
event envelope and local SQLite cursor, transition, bounded lease, and durable outbox state. It has
no probes, dispatcher, service activation, or Codex/Claude launcher. See
[Wakeful persistence prototype](wakeful.md).

`edge_monitor.py` is the first dependent producer for that neutral outbox. It classifies injected
systemd, journal, HTTP, and fixed authenticated-canary observations without importing the edge
runtime or performing host I/O. See [Wakeful edge-monitor prototype](wakeful-edge-monitor.md).
`edge_monitor_host.py` is its inert one-shot host/fixture adapter; it neither schedules itself nor
delivers the resulting neutral events.

Provider source and attachment tools are not part of either current MCP surface. Legacy task URLs
and GIDs resolve locally through the canonical alias table.

### Managed task-bound STDIO MCP

`mcp.py::build_server` owns the managed tool surface. Trusted launch state binds one active WorkId,
optional read-only reference WorkIds, the managed principal, run currentness, and any enabled write
gateways. Omitting a WorkId selects the active assignment; it is not workspace discovery. When
`SWITCHSTAND_MANAGED` is absent, `server_from_env` returns an unbound server with no task authority.
Managed construction reads canonical work and history from PostgreSQL and does not construct an
Asana client; retained mutation owners are supplied explicitly.

`mcp.py::build_context_server` is the smaller read-only context surface. It exposes only the
launch-bound work and its revision-checked history. Managed and ordinary MCPs reuse contracts and
state, but their authority and inventories are intentionally not interchangeable.

### Managed resource-worker runtime

`context.py` routes managed parent launches through `agent_broker.py`, `managed_launch.py`, and
`agent_executor.py`; direct managed Codex subprocess launch is not a production path. The broker
owns host-wide lease admission and durable status; the
executor applies a reserved lease through a sandboxed transient systemd unit; sealed prepared-parent
manifests bind the exact WorkId, current grant, reservation, attempt, unit, private writer/home, and
fixed headless Codex command. Admission uses available memory, PSI, recent swap movement, and an
atomic reservation ledger. Release requires identity-bound proof that the unit is terminal and the
exact cgroup's `cgroup.events` was positively read as `populated 0`, or an expired executor claim
whose sealed manifest and exact durable receipt prove that execution was never started. Missing,
mismatched, or ambiguous recovery evidence is `UNKNOWN`; cgroup absence is also `UNKNOWN`. An
expired lost-executor claim with an exact persisted starting receipt may release only after its
exact unit is terminal and its exact cgroup is positively empty; that unknown execution outcome is
released as cancelled.
Expired unattached reservations require positive proof that no launch was prepared. Native children
inherit the parent's aggregate cgroup but are not individually admitted.
The canary owns the bounded live qualification proof. Live qualification still requires that
separately authorized canary to prove limits,
read-only output, recursive cancellation and cleanup, pressure capture, and deterministic excess
worker denial. A failed or incomplete proof does not authorize retries or a live-proof claim.
See [Resource-governed agent workers](resource-governed-agent-workers.md).

### Development MCP

`context.py` also owns the ai-tools-only ordinary `--target-repo` prototype. CONTROL remains
Switchstand; `provision.py` admits the repository from one structured marker in provider-neutral
current work before target effects. The canonical target origin and fetched main bind a separate
private writer and repository-scoped Codex runtime. Task mode bindings prevent cross-repository
state reuse; dirty and committed progress resumes without reset. Dish retains its native environment.

`development.py` owns development-environment preparation and cleanup invoked by `launch.py`, plus
the development-only MCP and its exact linked-writer/run boundary. The MCP exposes four tools:
`check`, `commit_all_current_worktree`, `diagnostic_full_suite`, and `run_status`. The diagnostic
full-suite tool is explicitly non-authoritative; exact-head/composition GitHub Quality owns full
qualification. Preparation and cleanup are
module functions used by launch, not MCP tools. This surface is separate from product work authority.

## Durable state, identity, and currentness

PostgreSQL is the authoritative application state for identities, grants, effect recovery,
messaging, and required continuation:

- `failure_journal.py` owns append-only redacted failure attempts and resolutions;
  `pending_failures.py` owns the atomic offline queue and close/handoff gate. Stateful reads may
  project unresolved clearing actions, but the journal remains authoritative. The legacy
  `friction.md` source cannot be archived until import, read parity, queue synchronization, and an
  explicit cutoff all pass.
- `bootstrap_identity.py` and `bootstrap_adapters.py` own the temporary pre-migration launch
  compatibility boundary. It runs only after an exact canonical WorkId miss, verifies the immutable
  WorkId-to-Asana mapping and exact current provider task, and accepts only one already-current
  exact-principal launch grant. It never derives or rotates authority from provider content and is
  disabled by an authenticated migration-complete receipt.

- `canonical_work.py`, `canonical_relations.py`, and `work_events.py` own the explicit, inert
  compact zero-Asana schema definitions and repositories for current rows, stable title/completion
  search pages, legacy task aliases,
  dependencies, parents, simple project placements, and event history. `canonical_event_reads.py`
  projects DB event storage through the existing public history/event contracts without runtime
  wiring. `canonical_work_runtime.py` projects canonical current rows and relations through the
  existing get, search, create, scalar-update, parent, and dependency contracts. Protected creates
  atomically persist the row, requested parent or project placement, and effect receipt; protected
  parent/dependency changes atomically persist the relation and receipt. The repository's ordinary
  edge constructs this runtime directly; deployment and cutover remain separate effects.
  Migration `0008` materializes the compact tables; migration `0014_canonical_routing` adds the
  nullable canonical-root, owner, and next-action routing projection without inferring legacy
  values, and `0015_work_admission_time` adds nullable admission time. Native PostgreSQL creates
  set it from the server clock; preexisting and source-imported rows retain explicit `NULL` unless
  the import carries a trustworthy timestamp. `work_policy.py` alone validates resultant root, owner, wait, lifecycle, and next-action
  state for semantic writes; legacy incomplete rows remain editable through title/notes-only
  changes. The repositories remain outside shared
  `state.metadata` and are registered explicitly by the edge.
- `state.py` owns `work_handles` and `work_event_handles`, which bind provider work/events to stable
  WorkIds. `discovery.py` binds provider search and structure results before returning them.
  `outcome_state.py` separately owns append-only owner-local outcome snapshots and deterministic
  owner/Marco/dispatch action derivation. When `SWITCHSTAND_OUTCOME_STATE_ACTIONS=1`, the ordinary
  authenticated MCP surface admits one explicit target at its exact current revision, permits only
  AGENT-sourced snapshots, and projects that exact owner's actions on `work_get` and known
  `work_update` results. Stale summaries retain their stored actions but are not current execution
  or dispatch authority. The default-off switch removes the tool and result enrichment without
  deleting persisted revisions. This state does not own Stage 2 waits, dependencies, authorization,
  scheduling, activation, or implicit owner inference.
- `human_reviews.py` owns an inert exact-consequence/decision store; approval records readiness only.
  `human_review_shell.py` provides a default-off server-rendered Basic-auth and exact-Origin
  confirmation app over that store. Its public route, credentials, and production schema are not
  installed; no dispatch or activation is enabled.
- `human_trajectory.py` owns an inert, append-only record of bounded human-direction continuity.
  Its `RECORDED_HUMAN_DIRECTION` provenance is not implementation authorization, it has no public
  MCP wiring, and landing its schema does not activate process reliance or provider cutover.
- `grant_state.py` owns `work_grants` and `effect_intents`. `WorkGrant` in `grants.py` is the current
  caller authority contract; an operation ID identifies one protected effect across reconciliation.
- `messages.py` owns `messages`, `message_deliveries`, and the historical `message_projection`, including the
  AVAILABLE/RECEIVED/DISPOSITIONED lifecycle, result/effect evidence, and runtime-currentness
  checks. Message routes accept and create only the durable PostgreSQL message and delivery records;
  the historical projection table remains readable only for frozen-cutover disposition and later
  schema retirement.
- `agent_mailboxes.py` owns the temporary `agent_mailboxes` binding of a visible immutable agent
  name to an authenticated principal and hidden chat-session hash, with an independently generated
  endpoint UUID and generation. Endpoint UUIDs are message addresses, not WorkId identity; new
  endpoints are not inserted into `work_handles`, although pre-migration handle rows can remain as
  unreferenced legacy residue. For cross-principal recovery, the destination authenticated session
  records an exact preimage-bound request; the host-only `switchstand-agent-mailbox-transfer`
  command approves it in one transaction. It preserves the endpoint and deliveries, increments
  generation, fences the old principal/session, and fails closed if the mailbox or destination
  changed. Ordinary same-principal `agent_takeover` remains unchanged; there is no public approval
  tool. `agent_messages.py` supplies
  public name-based views over `MessageState`; migration `0016_agent_mailbox_transfers` owns audit.
- `lifecycle.py` owns `lifecycle_obligations`, the durable required-result continuation state.

The agent mailbox layer is current product behavior, but it is a compatibility bridge rather than
the final identity model. It reuses the UUID-shaped sender and recipient columns in `MessageState`
without asserting WorkId identity or requiring or creating provider bindings. Migrated installations
can retain the legacy handle residue described above. The HTTP edge reads exactly one hidden host
identity—ChatGPT `openai/session` unchanged or Codex `threadId` namespaced as `codex:<id>`—and fails
closed on missing, invalid, or ambiguous metadata; the mailbox stores its hash and binds one visible
name per principal-and-chat pair.
Principal, chat identity, visible name, mailbox
generation, MCP session currentness, and WorkId remain distinct. It should be retired or reshaped
only after the canonical agent/work identity model and migration of outstanding mailbox state are
implemented and verified.

Message receipt is fenced twice. The mailbox generation fences trusted principal takeover;
`RuntimeCurrentness` fences replacement MCP sessions or managed runs. Recovery is an explicit
transition. A new session or caller must not silently continue a RECEIVED delivery.

## Protected effects and UNKNOWN

The canonical runtime and `effects.py::CanonicalAppendGateway` own ordinary PostgreSQL work
mutations. The following legacy gateways remain only for managed/source compatibility and the
frozen-cutover disposition of provider-era effects; they are not ordinary PostgreSQL write owners:

- `effects.py::AppendGateway` owns append intent, send, and exact story/task readback;
- `creates.py::CreateGateway` owns creation and binding of the resulting provider work;
- `updates.py::UpdateGateway` owns scalar work updates;
- `relations.py::RelationGateway` owns dependency, hierarchy, assignment, and placement changes;
- `provider.py::AsanaProvider` owns how each operation is expressed and verified in Asana.

Asana provider reads retry only bounded transient transport, throttling, and server failures. They
emit sanitized internal failure classification (operation class, HTTP status or exception type, and
attempt count) without provider identifiers, response bodies, credentials, or changing the bounded
public `provider_error` contract. Provider writes are never retried by this read policy.
Definite write nonapplication retains the existing provider-neutral status/effect contract and a
closed sanitized reason: caller-invalid input, local admission denial, provider authority denial,
provider transient rejection, provider permanent rejection, or the legacy unclassified rejection.
The provider assigns a narrower reason only from trusted local validation or an unambiguous HTTP
status class; it never exposes response bodies or infers precision from provider prose. An ambiguous
transport or server outcome remains `UNKNOWN`, so diagnostics cannot weaken the effect journal or
authorize a blind resend.

The checks and recovery path are operation-specific. Append validates the bound target's canonical,
nonterminal state and observed revision; update and relation validate a bound canonical target and
its observed revision. Create has no observed revision: its contract requires exactly one parent or
project target, parent creation requires a granted, bound, canonical, nonterminal parent, and project
creation requires workspace scope plus an admitted project. Every gateway persists the operation ID
and durable prepared intent before a possible send. Create, update, and relation reconcile ambiguity
only through their explicit provider recovery or readback paths; append has no equivalent recovery
search and can retain an unresolved `UNKNOWN` as the durable barrier. `UNKNOWN` means a send may have
happened or recovery state is unreadable; it is not permission to create a new operation and resend.
When that barrier blocks a later authorized request, the later request remains explicitly `not_sent`
and names the older blocking operation. Update and relation gateways verify the caller's current
grant, write access, operation, version, and qualification before disclosing that blocker; denied
callers receive no blocking-operation detail. The DB-native ordinary API exposes no exact recovery
route for a provider-era effect. Blocker guidance directs every such effect to trusted frozen-cutover
adjudication without resend. The exact pre-cutover deployment performs any provider readback; a
separately authorized operator action archives or deletes the adjudicated rows before the DB-native
application is installed.
An applied result requires the operation's authoritative provider readback to match the intended
change. Changes to provider relation behavior therefore normally require coordinated edits to the
provider implementation, the relation gateway contract only when its provider-neutral semantics
change, and causal tests for both layers.

## Repository transport

`repository_bundle.py` resolves the ordinary `repository_bundle_get` transport. The rolling GitHub
release is only a cache: it contains a bundle, manifest, and checksum published by
`.github/workflows/repository-bundle.yml` after the Quality workflow. Resolution independently reads
the repository's current branch refs and returns `current` only when the authoritative ref map,
manifest digest, release asset digest, and checksum agree. A transition returns `refresh_pending`;
provider failure returns `unavailable`.

The bundle can transport branch objects and allow an exact requested SHA to be checked out, but the
release tag and manifest do not become source authority. GitHub branch refs remain authoritative,
and consumers must verify the advertised SHA-256 before materializing the bundle.

`repository_candidate.py` owns the ordinary read-only GitHub candidate qualification. It binds the
current public pull-request base and head to the merge-ref commit's ordered parents, then evaluates
the four exact-head/composition job names in its versioned code-owned catalogue. Missing, stale,
ambiguous, skipped, or wrong-subject evidence fails closed; provider failure is `UNKNOWN`. Compact
gate identity, reason, and timing are returned by default, while bounded failed-step and check-output
detail is opt-in. This provisional preferred read neither authorizes nor performs review, merge,
ruleset, credential, provider-write, or rollout effects; raw public GitHub reads remain a diagnostic
fallback during qualification of the semantic path.

## Launch, candidate, and host control

`launch.py` owns managed launch orchestration: linked-writer validation, exact-revision preflight,
development preparation, process supervision, and cleanup requests. `codex_runtime.py` owns Codex
command construction, configuration/readback, and managed runtime profile validation.
`run.py` supplies durable run identity/currentness checks.

For isolated launch, host-side `launch_source.py` resolves a WorkId or legacy task reference through
canonical PostgreSQL work and reads the protected source request from its notes;
`candidate.py::prepare_launch_source` verifies and materializes the exact Git repository/ref before
preflight. This is a transitional trusted-host bridge, not a provider API for agent code.

The materialized host launcher installed by `scripts/install-codex-shim` owns global `codex`
routing. Outside the canonical Switchstand Git common directory it directly launches the real Codex
binary, even when the checkout is missing or broken; inside that Git common directory it delegates
to `scripts/codex-dispatch`. The installed launcher is a regular host file, not a symlink into the
mutable checkout. The launcher exports a private `CODEX_INSTALL_DIR` before either route, so Codex's
automatic updater maintains its otherwise-unused visible command outside the managed launcher path.
`coordinator_sync.py` owns the hardened canonical-main Git boundary used by both in-session
synchronization and Coordinator replacement. Its read operation observes the fixed canonical
checkout and exact remote-main SHA; its write operation accepts only that SHA, revalidates it,
refuses dirty or divergent state, and performs an ancestor-only fast-forward. The private handoff
operation holds the same serialization/config boundary through final start-commit ancestry proof.
It accepts no caller-selected repository, remote, branch, or ref and is absent from the ordinary
ChatGPT MCP surface.
`scripts/coordinator-handoff` owns only replacement orchestration around that Git boundary: it
validates the exact launch-record and obligations inputs, delegates canonical-main synchronization
to `coordinator_sync.py`, and registers a pending handoff in private durable state. Plain
Coordinator launches remain independent and never claim it. Automatic transfer is disabled until a
separate explicit addressed claim can bind the handoff to a launcher-proven generation. Obligations
do not transfer and the old generation does not retire before the successor acknowledges that
future binding. Failure evidence is retained and never rewinds the safely advanced primary.
The control validates the code-owned GitHub origin, uses a fixed HTTPS source, admits only a small
positive allowlist of inert repository configuration, and runs fixed Git commands with caller/global
configuration, hooks, filters, fsmonitor, pagers, credentials, alternate protocols, and recursive
submodule fetch disabled or rejected.
This control only synchronizes Git; it does not refresh session-frozen controls, launch a successor,
deploy, or activate anything.
`codex-dispatch` creates a byte-identical pair for each launch: an immutable policy snapshot used
as frozen-control evidence and a uniquely named runtime profile that Codex may persist normal
session preferences into. The manifest records the snapshot digest and the runtime profile's
launch-time digest; currentness rechecks only the immutable snapshot, never the mutable runtime
copy. This receipt is launch-manifest schema v3; the checker still accepts valid legacy v1/v2
manifests; changed controls report bounded recheck boundaries instead of permanent stale, while missing or corrupt proof remains `CURRENTNESS_UNKNOWN`. It also records the
executable, repository controls and
privacy-preserving invocation identity in the per-generation manifest owned by
`scripts/coordinator-control`. Concurrent launches retain the shared Coordinator home, authentication,
session storage and byte-stable hooks without replacing another generation's evidence. The full
local Markdown dependency graph rooted at `AGENTS.md` remains tracked for post-sync change
detection, while schema v3 records a separate bounded post-compaction reread set. A synchronous
`SessionStart` hook for `source=compact` runs generation-private launch-time snapshots of both the
wrapper and checker, then injects the result as immediate developer context before the continuation;
the snapshotted checker still compares the tracked canonical controls. Each unresolved tracked dependency retains its identity and reason
as component-scoped `CURRENTNESS_UNKNOWN`.
That directory is appended to the child `PATH` after the managed launcher, preventing the standalone
installer from rewriting its shell profile block while keeping raw `codex` resolution on the shim.
The installer also materializes a private repair installer/source under
`~/.local/state/switchstand/codex/shim` and enables a user-systemd path watch on the visible
command plus a five-minute timer on the same idempotent repair service. The watch repairs
manual or non-inheriting installer replacements promptly; the timer catches replacements missed
during service execution and watch rearm without depending on the checkout. Healthy checks
preserve launcher identity.
The repository dispatcher creates a generation-owned linked writer and launches Codex there with a separate Coordinator home, shared authentication, a durable per-launch starting-commit file named in developer context, and the repository's fixed runtime policy. Canonical `main` may advance without rewriting that exact candidate; repository mutations stay in the writer while per-generation and byte-stable shared guards protect the primary. `scripts/codex-coordinator-profile` copies
only the allowlisted benign user preferences into that isolated profile and installs no conventional
user-level instructions. It separately copies only the repository-owned canonical `switchstand`
HTTP/OAuth MCP contract, makes that MCP required for Coordinator launch, and adds the narrow
Coordinator-control MCP; unrelated user MCP configuration remains excluded. The same per-launch renderer defaults role-neutral root continuity to
`PILOT` with `ASSIGNMENT` lifetime, adding only root `UserPromptSubmit` and `Stop` handlers backed by
`scripts/codex-continuity-hook`. The explicit launch-only
`SWITCHSTAND_CODEX_CONTINUITY=OFF` selector disables those continuity handlers. Each generation's
immutable profile freezes and exposes the resolved continuity/lifetime modes, while launch selectors
are removed before Codex starts. The shared `hooks.json` remains the byte-stable destructive guard,
so concurrent OFF and PILOT launches cannot replace one another's continuity policy. The separate hook's blocked-stop response enumerates the exact terminal deliberate-yield markers accepted for the current lifetime, writes minimal
private per-run Stop telemetry, and fails open visibly on malformed input or local errors. It never
registers `SubagentStop` or changes the destructive-command guard.
Because an exact writable file root can be misclassified as a directory by sandbox child-mount
handling, dispatch gives new Coordinators a dedicated mode-0700 local-state friction directory and
binds their repository `friction.md` path to its mode-0600 file with a validated symlink. The first
new launch copies a valid legacy local-state `friction.md` into that directory without moving or
deleting the legacy file, so already-running agents remain undisturbed. Outside the canonical repository,
dispatch passes through to the ordinary Codex executable.

`development.py` owns high-level environment/workload behavior, including invoking the repository-owned
`scripts/docker-gc` BuildKit cache cap whenever managed Docker work is reconciled. `scripts/docker-gc` serializes
host-wide cache reconciliation and bounds only unused build cache; exact resource owners remain responsible for
their images, containers, networks, and volumes. `docker.py` owns shared low-level
Docker inspection, naming, labels, and removal primitives. Launch decides when those operations run;
the remaining overlap is a known lifecycle-policy convergence boundary, not evidence of two equal
owners.

Git landing verification follows the repository's merge-commit model. For a direct or rebased
candidate, the landing commit has the exact current reviewed base and candidate as its ordered
parents, the candidate descends from that base, and the landing tree equals the candidate tree. When
an immutable candidate is preserved after target advancement, it retains its original reviewed-base
ancestry; the landing commit instead has the exact current base and immutable candidate as its
ordered parents, and its tree equals the exact qualified composition tree.

## Operator provisioning surface

`rehearsal_target.py` owns the explicit `switchstand-rehearsal-target` lifecycle for one
descriptor-bound copied-state migration target. It creates only canonical private rehearsal
roots and namespaced disposable Docker resources; teardown revalidates descriptor identity and
Compose labels before removing that namespace. Its default network is pre-created on the canonical
deterministic development `/24`, and the descriptor binds the exact subnet and Docker IPAM.
Provisioning failures are durable and bounded;
their exact `FAILED` descriptor is also the fail-closed cleanup authority for zero or one proven
namespaced resources. It never selects or changes production state.

`zero_asana_hold.py` owns the transition-only integrated production hold: exact host edge gate and
stop, create-new FastMCP snapshot, and one PostgreSQL transaction whose SHARE locks span source
capture/export/readback and the injected cutover continuation. Direct Asana writes remain a stated
coordination limitation rather than a technical freeze. Its terminal receipt binds the stopped
systemd process/listener proof and PostgreSQL database, user, backend, timeout, held-lock, and
waiting-writer proof; a failed post-commit receipt is explicitly `UNKNOWN`.

## Where to edit

| Intended change | Primary owner | Usually inspect or update with it |
| --- | --- | --- |
| Add or change an ordinary MCP tool | `chatgpt_mcp.py::build_ordinary_tools` | `chatgpt.py`, closed contracts, HTTP-edge inventory tests, and repository MCP allowlisting/configuration |
| Change HTTP authentication, bind, or session wiring | `chatgpt_edge.py` | edge process/auth tests and deployment configuration; do not duplicate tool semantics here |
| Change production edge replacement sequencing | `edge_maintenance.py` | maintenance transaction tests and `chatgpt-mcp-edge.md`; keep landing inert and activation separate |
| Change shared private byte-file durability mechanics | `secure_file.py` | edge maintenance and rehearsal callers; keep schemas, state machines, path policy, and domain errors local |
| Change first combined-to-split activation sequencing | `stable_auth_deployment.py` | stable-auth deployment/migration/host tests and `chatgpt-mcp-edge.md`; preserve the public gate and never rewind possibly-written OAuth state |
| Change inert Wakeful event persistence | `wakeful.py` | `wakeful.md`; keep probe and agent adapters outside the neutral contract |
| Change inert edge-monitor classification | `edge_monitor.py` | `wakeful-edge-monitor.md`; do not add host activation or agent dispatch here |
| Change inert edge-monitor host qualification | `edge_monitor_host.py` | `wakeful-edge-monitor.md`; keep scheduling and delivery outside it |
| Change provider-neutral work discovery or WorkId binding | `discovery.py`, `state.py` | `chatgpt.py`, provider search/structure implementation, migrations when storage changes |
| Change the inert compact zero-Asana work/relations/event model | `canonical_work.py`, `canonical_relations.py`, `work_events.py`, `canonical_event_reads.py`, `canonical_work_runtime.py` | explicit canonical metadata, migrations `0008`, `0014_canonical_routing`, and `0015_work_admission_time`, real-PostgreSQL repository tests, public projection tests, default-off service wiring, and protected create/update/relation atomicity |
| Change recorded human-direction continuity | `human_trajectory.py` | migration `0009` and trajectory tests; preserve append-only provenance without turning it into implementation authority |
| Change private flow evidence reporting | `flow_report.py` | exact WorkId database correlation and source coverage; no journal reader, external inference, mutation, or process control |
| Change Asana payload or relation semantics | `provider.py::AsanaProvider` | `relations.py` when the provider-neutral contract changes, plus provider and gateway tests |
| Change protected-effect recovery or UNKNOWN behavior | the relevant gateway and `grant_state.py` | provider readback implementation and causal ambiguity/retry tests |
| Change message lifecycle/currentness | `messages.py` | managed and ordinary adapters; `agent_mailboxes.py` for name/principal/generation binding changes |
| Change managed launch or runtime behavior | `launch.py`, `codex_runtime.py`, `run.py` | `candidate.py`, `launch_source.py`, development lifecycle, and launch/runtime tests according to the boundary touched |
| Change repository bundle freshness or publication | `repository_bundle.py`, `.github/workflows/repository-bundle.yml` | `repository_bundle_get`, bundle resolver tests, and bootstrap guidance |
| Change development workloads or cleanup | `development.py` | `docker.py`, development MCP scripts, and real-Docker qualification when the claim crosses that boundary |
| Change copied-state rehearsal provisioning | `rehearsal_target.py` | `development.md`, zero-Asana qualification, and disposable Docker ownership tests |
| Change resource-worker admission, execution, or live proof | `agent_broker.py`, `agent_executor.py`, `agent_canary.py` | [resource-worker architecture](resource-governed-agent-workers.md), owner-local tests, host pressure/sandbox boundaries, and the separate activation decision |
| Change affected-test selection or repository qualification | `affected_tests.py`, `repository_candidate.py` | their owner-local tests, `scripts/check`, GitHub Quality/composition evidence, and exact-head-versus-local truth |
| Change global raw-Codex routing or installation | `scripts/codex-shim`, `scripts/install-codex-shim` | shim tests and the ordinary Coordinator entry contract |
| Change global raw-Claude routing or installation | `scripts/claude-shim`, `scripts/install-claude-shim`, `scripts/claude-dispatch`, `.claude/coordinator-*.json` | `tests/test_claude_shim.py` and the Coordinator hook tests |
| Change Coordinator ordinary launch policy | `scripts/codex-dispatch` | `scripts/codex-coordinator-profile` and their tests |
| Change guarded canonical-main synchronization | `coordinator_sync.py` | generated Coordinator profile, stdio boundary tests, and `scripts/coordinator-handoff` interaction |

## Compatibility and retirement boundaries

Transition-only surfaces stay until their exact recovery and reliance evidence is terminal. These
are removal predicates, not removal decisions:

| Transition surface | Why keep it now | Evidence required before removal | Durable retirement owner |
| --- | --- | --- | --- |
| Copied-state rehearsal (`rehearsal_target.py`) | Supplies the disposable real boundary for zero-Asana qualification and bounded cleanup. | The required composed rehearsal evidence is retained and every READY/FAILED target is either intentionally retained with an owner or has teardown proof; a replacement qualification boundary must cover the same claims before deletion. | Rehearsal owner (`rehearsal_target.py`, `development.md`) |
| Stable-auth activation-only migration/deployment (`stable_auth_migration.py`, `stable_auth_deployment.py`) | Owns state-copy receipts, the public gate, first split activation, and ambiguous/post-exposure forward repair. | The topology decision and separately authorized activation are terminal, exact live readback has closed every migration/deployment receipt and rollback window, and the retained topology has another owner for every still-required recovery path. | Stable-auth transition owner (`stable_auth_deployment.py`, `chatgpt-mcp-edge.md`) |
| Resource-worker broker/executor/canary trial | Keeps the inert candidate available for the bounded evidence needed to adopt or retire it. | A separately authorized exact canary is terminal and the explicit decision either assigns an active runtime owner after PASS or records retirement plus disposal of broker state/units after non-adoption; incomplete/UNKNOWN evidence permits neither. | Resource-worker trial owner (`agent_broker.py`, `resource-governed-agent-workers.md`) |

- Raw `source_*` tools remain until ordinary, recovery, and failback consumers have verified
  provider-neutral replacements and legacy references are drained.
- Agent-name mailboxes remain until canonical durable agent identity replaces their endpoint UUID
  bridge and existing messages can be migrated without losing currentness or reply identity.
- `launch_source.py` remains until isolated launch no longer needs host-side source translation and
  the replacement preserves exact-source and authority checks.
- `scripts/switchstand-context` and `scripts/switchstand-start` are compatibility wrappers around
  current entry paths, not owners of runtime semantics.
- Provider IDs and credentials stay inside trusted provider and launch adapters. They are not
  normal agent-facing authority.
- `marcogallotta/switchstandold` is read-only evidence, never an implementation base or architectural
  ancestor.

`codex_wakeful.py` owns the opt-in Codex technical precursor: exact start-record/generation
binding, delivery/child source references, client-ID admission and private `codex-wakeful.json`
projection. Its explicit probe excludes simultaneous probes with a nonblocking generation-token
lock. For external-source admission it starts one bounded, lazy stdio client using the exact
supplied Codex binary and `CODEX_HOME`, writes the deterministic client ID through Codex's durable
thread queue, and terminates only that owned child. It never starts, stops, restarts, attaches to,
or owns a managed Codex daemon. The live same-home embedded root discovers durable queue changes
and consumes them when idle. `wakeful.py` remains the neutral SQLite/outbox owner; ordinary
launcher behavior does not invoke the precursor.
The default-off `run_inbound` pilot reuses existing authorized `MessageState` and
`AgentMailboxState` objects in a dedicated supervised host process. `wakeful_runner.py` is its
production supervision composition: an explicitly invoked process loads one private frozen
mailbox/binding configuration, reuses `chatgpt_edge.resource_service()`, and stops through
SIGINT/SIGTERM. Its user-systemd renderer is inert and does not install, enable, start, register,
or take over anything. The runner creates no database or service owner and leaves the probe CLI
semantics in `codex_wakeful.py` unchanged. It reads only committed
delivery references for one explicitly configured mailbox endpoint/generation/session matched
to the exact Codex root/start record. Source transactions finish before host admission so
host latency cannot block canonical receive/disposition/takeover. Source and runtime checks
are preflight, not atomic fences against concurrent source changes or host turns; the awakened
agent must reread the exact delivery before acting. Busy targets retain the durable queued input
until idle; unsupported history and ambiguous attempts remain UNKNOWN without resend. Queue and
consumed-history readback remain distinct. A home-wide private lock excludes all
precursor writers, and the source is never received or dispositioned by intake. Stopping the
process preserves source records and the private admission projection; restarting the same
target scans the pending source again. This pilot does not supply the neutral product bridge,
operator lifecycle, retry/escalation, other sources/clients, or unattended service operation.
