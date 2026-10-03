# Architecture

Switchstand is a bounded, provider-neutral work controller. Stable `WorkId` values are the
application identity for work; provider task IDs stay behind trusted state and provider adapters.
The repository has several runtime surfaces with deliberately different authority. A tool being
implemented in one surface does not make it available or authorized in another.

## Semantic owners and request flow

The ordinary authenticated flow is:

1. `chatgpt_edge.py` authenticates the HTTP caller, creates the runtime dependencies, and registers
   the canonical ordinary tools produced by `chatgpt_mcp.py::build_ordinary_tools`.
2. `chatgpt.py::ChatGPTService` resolves caller admission and coordinates provider-neutral reads or
   the appropriate protected gateway.
3. `state.py::PostgresState` translates stable WorkIds to provider bindings. `provider.py` owns the
   Asana protocol, payloads, pagination, and provider readback semantics.
4. Protected gateways in `effects.py`, `creates.py`, `updates.py`, and `relations.py` validate the
   current principal/grant and preserve effect identity before calling the provider.
5. `grant_state.py` journals effect intent and outcome. A success is returned only after the
   gateway's authoritative readback establishes the requested result.

`contracts.py` owns shared closed request/result models. `core.py::Controller` is the bounded work
read/source controller used by managed launches and by the exact-source compatibility methods; it
does not own ordinary HTTP authentication or grant issuance.

## Runtime surfaces

### Authenticated ordinary HTTP MCP

`chatgpt_mcp.py::build_ordinary_tools` is the canonical definition of the ordinary tool inventory
and behavior. It includes provider-neutral work discovery, structure, history, attachments and
events; protected append/create/update/relation operations; registered-name `agent_message_*`
messaging; required-result persistence; and repository bundle transport. Managed task-bound
runtimes separately retain the WorkId-addressed `message_*` family.

`chatgpt_edge.py` is the HTTP/OAuth edge, not a second tool definition. Its
`oauth_continuity.py` provider restricts authentication to the configured GitHub user and keeps one
validated upstream GitHub credential behind a stable storage identity; downstream client JWTs,
JTIs, grants, and runtime identities remain distinct. Legacy JTI mappings converge lazily to that
credential. A still-valid signed downstream token can rebuild a missing or expired JTI mapping only
while its signed claims remain valid, its stored client metadata remains current for refresh, and
that canonical upstream credential still passes the configured identity and scope checks. Explicit
and transparent refresh share one process-local serialization boundary.
The edge bridges the authenticated request into a `RequestPrincipal`, builds
the Asana/PostgreSQL-backed service, registers every ordinary tool with FastMCP, and supplies the
MCP session ID used for message-currentness fencing. Repository MCP configuration and deployment
configuration must expose the same intended inventory, but tool semantics belong in
`build_ordinary_tools`. The edge also owns the narrow HTTP lifecycle integration that completes a
standalone Streamable HTTP GET when the SSE dependency returns during shutdown.

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
are idempotent under their stable identity or transition contracts except
`agent_project_bootstrap`: its applied provider writes can return UNKNOWN without a stable
operation identity and must not be blindly retried. Registration rejects a new ordinary tool until
that classification is extended.

`edge_maintenance.py` owns the service-specific maintenance-window transaction for replacing this
edge. It gates all public Switchstand MCP/OAuth routes in Caddy before shutdown, snapshots FastMCP
transport state only while the service is offline, swaps the launcher atomically, verifies locally
before ungating, and preserves `UNKNOWN` behind the gate. Its exact receipt runner consumes a
read-only host-phase reconciler that revalidates runtime and artifact trust boundaries and accepts
only the durable phase or one exactly proven next state. The receipt keeps the irreversible
Postgres-authority fact across later host phases. It does not own tool semantics, OAuth state
format, host installation, or activation authority.

`wakeful.py` is an inert, agent-system-neutral event persistence prototype. It owns the sanitized
event envelope and local SQLite cursor, transition, bounded lease, and durable outbox state. It has
no probes, dispatcher, service activation, or Codex/Claude launcher. See
[Wakeful persistence prototype](wakeful.md).

`edge_monitor.py` is the first dependent producer for that neutral outbox. It classifies injected
systemd, journal, HTTP, and fixed authenticated-canary observations without importing the edge
runtime or performing host I/O. See [Wakeful edge-monitor prototype](wakeful-edge-monitor.md).
`edge_monitor_host.py` is its inert one-shot host/fixture adapter; it neither schedules itself nor
delivers the resulting neutral events.

`source_task`, `source_stories`, and `source_story` are not part of this ordinary surface. They
remain transitional managed compatibility reads for bounded legacy recovery/reference workflows.

### Managed task-bound STDIO MCP

`mcp.py::build_server` owns the managed tool surface. Trusted launch state binds one active WorkId,
optional read-only reference WorkIds, the managed principal, run currentness, and any enabled write
gateways. Omitting a WorkId selects the active assignment; it is not workspace discovery. When
`SWITCHSTAND_MANAGED` is absent, `server_from_env` returns an unbound server with no task authority.
Managed construction reads canonical work and history from PostgreSQL and does not construct an
Asana client; retained mutation owners are supplied explicitly to the unchanged tool surface.

`mcp.py::build_context_server` is the smaller read-only context surface. It exposes only the
launch-bound work and its revision-checked history. Managed and ordinary MCPs reuse contracts and
state, but their authority and inventories are intentionally not interchangeable.

### Inert resource-worker trial

`agent_broker.py`, `agent_executor.py`, and `agent_canary.py` are a default-off trial architecture,
not the active worker launcher. The broker owns host-wide lease admission and durable status; the
executor applies a reserved leaf lease through a sandboxed transient systemd unit; the canary owns
the bounded live proof. Adoption requires that separately authorized canary to prove limits,
read-only output, recursive cancellation and cleanup, pressure capture, and deterministic excess
worker denial. A failed or incomplete proof preserves the trial for diagnosis or retirement; it
does not authorize retries or partial reliance. The adoption-versus-retirement decision remains
explicit and evidence-gated. See [Resource-governed agent workers](resource-governed-agent-workers.md).

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

- `canonical_work.py`, `canonical_relations.py`, and `work_events.py` own the explicit, inert
  compact zero-Asana schema definitions and repositories for current rows, stable title/completion
  search pages, legacy task aliases,
  dependencies, parents, simple project placements, and event history. `canonical_event_reads.py`
  projects DB event storage through the existing public history/event contracts without runtime
  wiring. `canonical_work_runtime.py` projects canonical current rows and relations through the
  existing get, search, and scalar-update contracts; the ordinary edge constructs it default-off
  until explicit activation.
  Migration `0008` materializes the compact tables; the repositories and runtime projection remain
  outside shared `state.metadata`, unregistered, and inert.
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
- `human_trajectory.py` owns an inert, append-only record of bounded human-direction continuity.
  Its `RECORDED_HUMAN_DIRECTION` provenance is not implementation authorization, it has no public
  MCP wiring, and landing its schema does not activate process reliance or provider cutover.
- `grant_state.py` owns `work_grants` and `effect_intents`. `WorkGrant` in `grants.py` is the current
  caller authority contract; an operation ID identifies one protected effect across reconciliation.
- `messages.py` owns `messages`, `message_deliveries`, and `message_projection`, including the
  AVAILABLE/RECEIVED/DISPOSITIONED lifecycle, result/effect evidence, and runtime-currentness
  checks. Provider projection is optional and does not replace the durable message record.
- `agent_mailboxes.py` owns the temporary `agent_mailboxes` binding of a visible immutable agent
  name to an authenticated principal and hidden chat-session hash, with an independently generated
  endpoint UUID and generation. Endpoint UUIDs are message addresses, not WorkId identity; new
  endpoints are not inserted into `work_handles`, although pre-migration handle rows can remain as
  unreferenced legacy residue. `agent_messages.py` supplies public name-based views over the
  existing `MessageState` records.
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

The protected gateways are the semantic owners of provider writes:

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
callers receive no blocking-operation detail. `effect_reconcile` accepts only that OperationId, loads the durable
provider-neutral scalar-update intent internally, requires the same authenticated principal and
a current grant for the exact work and operation, and uses the existing operation-specific readback
path. Its inspection omits provider identifiers, principal keys, and qualification internals. It can
confirm the existing update or preserve `UNKNOWN`; relation and append recovery remain unsupported.
Blocker guidance therefore points to `effect_reconcile` only for scalar updates and directs relation
or append blockers to trusted operator adjudication without resend. It cannot retire, release, or
overwrite an unresolved effect, including an orphan whose stored intent does not match the provider.
Such adjudication remains an
unimplemented trusted-operator product decision.
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
That directory is appended to the child `PATH` after the managed launcher, preventing the standalone
installer from rewriting its shell profile block while keeping raw `codex` resolution on the shim.
The installer also materializes a private repair installer/source under
`~/.local/state/switchstand/codex/shim` and enables a user-systemd path watch on the visible
command plus a five-minute timer on the same idempotent repair service. The watch repairs
manual or non-inheriting installer replacements promptly; the timer catches replacements missed
during service execution and watch rearm without depending on the checkout. Healthy checks
preserve launcher identity.
The repository dispatcher launches Codex with a separate Coordinator home,
shared authentication, a durable per-launch starting-commit file named in developer context, and
the repository's fixed Coordinator runtime policy. That policy retains
promptless host development access while making the shared primary checkout read-only except for `friction.md` and the Git
metadata needed for fetch and linked-writer operation; a pre-tool guard rejects primary-checkout Git
mutations and first-class patches outside `friction.md`, and Edit-family writes into Claude auto-memory. `scripts/codex-coordinator-profile` copies
only the allowlisted benign user preferences into that isolated profile and installs no conventional
user-level instructions. Because the Linux sandbox cannot carve out one writable file below a
read-only directory, dispatch preserves the ignored `friction.md` content in Switchstand's local
state and binds the repository path to it with a validated symlink. Outside the canonical repository,
dispatch passes through to the ordinary Codex executable.

`development.py` owns high-level environment/workload behavior, including invoking the repository-owned
`scripts/docker-gc` BuildKit cache cap whenever managed Docker work is reconciled. `scripts/docker-gc` serializes
host-wide cache reconciliation and bounds only unused build cache; exact resource owners remain responsible for
their images, containers, networks, and volumes. `docker.py` owns shared low-level
Docker inspection, naming, labels, and removal primitives. Launch decides when those operations run;
the remaining overlap is a known lifecycle-policy convergence boundary, not evidence of two equal
owners.

Git landing verification follows the repository's merge-commit model: the landing commit must have
the reviewed base and reviewed candidate as its two ordered parents, the candidate must descend from
the reviewed base, and the landing tree must equal the reviewed candidate tree.

## Operator provisioning surface

`rehearsal_target.py` owns the explicit `switchstand-rehearsal-target` lifecycle for one
descriptor-bound copied-state migration target. It creates only canonical private rehearsal
roots and namespaced disposable Docker resources; teardown revalidates descriptor identity and
Compose labels before removing that namespace. Its default network is pre-created on the canonical
deterministic development `/24`, and the descriptor binds the exact subnet and Docker IPAM.
Provisioning failures are durable and bounded;
their exact `FAILED` descriptor is also the fail-closed cleanup authority for zero or one proven
namespaced resources. It never selects or changes production state.

`durable_agent_project.py` backs the `switchstand-bootstrap-agent-project` command and the ordinary
MCP `agent_project_bootstrap` adapter. It is a retained, explicit operator utility for creating or reconciling one marked Asana role project, its ordered
CURRENT/WAITING/DEFERRED sections, the supplied custom fields, and a multihomed `AGENT MASTER` task.
It performs exact preflight and post-write readback and reports ambiguous post-write state as
UNKNOWN. It is not runtime mailbox storage, work discovery, or a general project-management API.

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
| Change the inert compact zero-Asana work/relations/event model | `canonical_work.py`, `canonical_relations.py`, `work_events.py`, `canonical_event_reads.py`, `canonical_work_runtime.py` | explicit canonical metadata, migration `0008`, real-PostgreSQL repository tests, public projection tests, default-off service wiring, and protected-update atomicity |
| Change recorded human-direction continuity | `human_trajectory.py` | migration `0009` and trajectory tests; preserve append-only provenance without turning it into implementation authority |
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
lock and connects only by WebSocket to an existing Codex-owned shared endpoint. `wakeful.py` remains the
neutral SQLite/outbox owner; ordinary launcher behavior does not invoke the precursor.
