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
events; protected append/create/update/relation operations; durable work-addressed and agent-name
messaging; required-result persistence; and repository bundle transport.

`chatgpt_edge.py` is the HTTP/OAuth edge, not a second tool definition. Its
`oauth_continuity.py` provider restricts authentication to the configured GitHub user and keeps one
validated upstream GitHub credential behind a stable storage identity; downstream client JWTs,
JTIs, grants, and runtime identities remain distinct. Legacy JTI mappings converge lazily to that
credential, and explicit and transparent refresh share one process-local serialization boundary.
The edge bridges the authenticated request into a `RequestPrincipal`, builds
the Asana/PostgreSQL-backed service, registers every ordinary tool with FastMCP, and supplies the
MCP session ID used for message-currentness fencing. Repository MCP configuration and deployment
configuration must expose the same intended inventory, but tool semantics belong in
`build_ordinary_tools`. The edge also owns the narrow HTTP lifecycle integration that completes a
standalone Streamable HTTP GET when the SSE dependency returns during shutdown.
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
before ungating, and preserves `UNKNOWN` behind the gate. It does not own tool semantics, OAuth
state format, candidate preparation, host installation, or activation authority.

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

`mcp.py::build_context_server` is the smaller read-only context surface. It exposes only the
launch-bound work and its revision-checked history. Managed and ordinary MCPs reuse contracts and
state, but their authority and inventories are intentionally not interchangeable.

### Development MCP

`development.py` owns development-environment preparation and cleanup invoked by `launch.py`, plus
the development-only MCP and its exact linked-writer/run boundary. The MCP exposes four tools:
`check`, `commit_all_current_worktree`, `quality`, and `run_status`. Preparation and cleanup are
module functions used by launch, not MCP tools. This surface is separate from product work authority.

## Durable state, identity, and currentness

PostgreSQL is the authoritative application state for identities, grants, effect recovery,
messaging, and required continuation:

- `state.py` owns `work_handles` and `work_event_handles`, which bind provider work/events to stable
  WorkIds. `discovery.py` binds provider search and structure results before returning them.
- `grant_state.py` owns `work_grants` and `effect_intents`. `WorkGrant` in `grants.py` is the current
  caller authority contract; an operation ID identifies one protected effect across reconciliation.
- `messages.py` owns `messages`, `message_deliveries`, and `message_projection`, including the
  AVAILABLE/RECEIVED/DISPOSITIONED lifecycle, result/effect evidence, and runtime-currentness
  checks. Provider projection is optional and does not replace the durable message record.
- `agent_mailboxes.py` owns the temporary `agent_mailboxes` binding of a visible immutable agent
  name to an authenticated principal and hidden chat-session hash, with a synthetic mailbox WorkId
  and generation. `agent_messages.py` supplies public name-based views over the existing
  `MessageState` records.
- `lifecycle.py` owns `lifecycle_obligations`, the durable required-result continuation state.

The agent mailbox layer is current product behavior, but it is a compatibility bridge rather than
the final identity model: it reuses work-addressed message storage by creating `agent-mailbox`
handles. The HTTP edge reads exactly one hidden host identity—ChatGPT `openai/session` unchanged or
Codex `threadId` namespaced as `codex:<id>`—and fails closed on missing, invalid, or ambiguous
metadata; the mailbox stores its hash and binds one visible name per principal-and-chat pair.
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

The checks and recovery path are operation-specific. Append validates the bound target's canonical,
nonterminal state and observed revision; update and relation validate a bound canonical target and
its observed revision. Create has no observed revision: its contract requires exactly one parent or
project target, parent creation requires a granted, bound, canonical, nonterminal parent, and project
creation requires workspace scope plus an admitted project. Every gateway persists the operation ID
and durable prepared intent before a possible send. Create, update, and relation reconcile ambiguity
only through their explicit provider recovery or readback paths; append has no equivalent recovery
search and can retain an unresolved `UNKNOWN` as the durable barrier. `UNKNOWN` means a send may have
happened or recovery state is unreadable; it is not permission to create a new operation and resend.
When that barrier blocks a later request, the later request remains explicitly `not_sent` and names
the older blocking operation. `effect_reconcile` accepts only that OperationId, loads the durable
provider-neutral scalar-update intent internally, requires the same authenticated principal and
a current grant for the exact work and operation, and uses the existing operation-specific readback
path. Its inspection omits provider identifiers, principal keys, and qualification internals. It can
confirm the existing update or preserve `UNKNOWN`; relation and append recovery remain unsupported.
It cannot retire, release, or overwrite an unresolved effect. Such adjudication remains an
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

## Launch, candidate, and host control

`launch.py` owns managed launch orchestration: linked-writer validation, exact-revision preflight,
development preparation, process supervision, and cleanup requests. `codex_runtime.py` owns Codex
command construction, configuration/readback, and managed runtime profile validation.
`run.py` supplies durable run identity/currentness checks.

For isolated launch, host-side `launch_source.py` reads and validates the protected source request;
`candidate.py::prepare_launch_source` verifies and materializes the exact Git repository/ref before
preflight. This is a transitional trusted-host bridge, not a provider API for agent code.

The materialized host launcher installed by `scripts/install-codex-shim` owns global `codex`
routing. Outside the canonical Switchstand Git common directory it directly launches the real Codex
binary, even when the checkout is missing or broken; inside that Git common directory it delegates
to `scripts/codex-dispatch`. The installed launcher is a regular host file, not a symlink into the
mutable checkout. The repository dispatcher launches Codex with a separate Coordinator home,
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

`development.py` owns high-level environment/workload behavior. `docker.py` owns shared low-level
Docker inspection, naming, labels, and removal primitives. Launch decides when those operations run;
the remaining overlap is a known lifecycle-policy convergence boundary, not evidence of two equal
owners.

Git landing verification follows the repository's merge-commit model: the landing commit must have
the reviewed base and reviewed candidate as its two ordered parents, the candidate must descend from
the reviewed base, and the landing tree must equal the reviewed candidate tree.

## Operator provisioning surface

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
| Change inert Wakeful event persistence | `wakeful.py` | `wakeful.md`; keep probe and agent adapters outside the neutral contract |
| Change inert edge-monitor classification | `edge_monitor.py` | `wakeful-edge-monitor.md`; do not add host activation or agent dispatch here |
| Change inert edge-monitor host qualification | `edge_monitor_host.py` | `wakeful-edge-monitor.md`; keep scheduling and delivery outside it |
| Change provider-neutral work discovery or WorkId binding | `discovery.py`, `state.py` | `chatgpt.py`, provider search/structure implementation, migrations when storage changes |
| Change Asana payload or relation semantics | `provider.py::AsanaProvider` | `relations.py` when the provider-neutral contract changes, plus provider and gateway tests |
| Change protected-effect recovery or UNKNOWN behavior | the relevant gateway and `grant_state.py` | provider readback implementation and causal ambiguity/retry tests |
| Change message lifecycle/currentness | `messages.py` | managed and ordinary adapters; `agent_mailboxes.py` for name/principal/generation binding changes |
| Change managed launch or runtime behavior | `launch.py`, `codex_runtime.py`, `run.py` | `candidate.py`, `launch_source.py`, development lifecycle, and launch/runtime tests according to the boundary touched |
| Change repository bundle freshness or publication | `repository_bundle.py`, `.github/workflows/repository-bundle.yml` | `repository_bundle_get`, bundle resolver tests, and bootstrap guidance |
| Change development workloads or cleanup | `development.py` | `docker.py`, development MCP scripts, and real-Docker qualification when the claim crosses that boundary |
| Change global raw-Codex routing or installation | `scripts/codex-shim`, `scripts/install-codex-shim` | shim tests and the ordinary Coordinator entry contract |
| Change global raw-Claude routing or installation | `scripts/claude-shim`, `scripts/install-claude-shim`, `scripts/claude-dispatch`, `.claude/coordinator-*.json` | `tests/test_claude_shim.py` and the Coordinator hook tests |
| Change Coordinator ordinary launch policy | `scripts/codex-dispatch` | `scripts/codex-coordinator-profile` and their tests |

## Compatibility and retirement boundaries

- Raw `source_*` tools remain until ordinary, recovery, and failback consumers have verified
  provider-neutral replacements and legacy references are drained.
- Agent-name mailboxes remain until canonical durable agent identity replaces their synthetic
  WorkId bridge and existing messages can be migrated without losing currentness or reply identity.
- `launch_source.py` remains until isolated launch no longer needs host-side source translation and
  the replacement preserves exact-source and authority checks.
- `scripts/switchstand-context` and `scripts/switchstand-start` are compatibility wrappers around
  current entry paths, not owners of runtime semantics.
- Provider IDs and credentials stay inside trusted provider and launch adapters. They are not
  normal agent-facing authority.
- `marcogallotta/switchstandold` is read-only evidence, never an implementation base or architectural
  ancestor.
