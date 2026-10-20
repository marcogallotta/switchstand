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
Fixed reporting tools route bugs, missing features, process complaints, avoidable performance
latency, Human Review feedback, and the all-event Fork Management trial to separate
server-selected canonical projects. Fork Management reuses this reporting path for assignments,
timestamped checks (including no findings), and outcomes under the
[supervision contract](coordinator-tracker-contract.md#fork-management-feedback-trial).
They share one
private create path with explicit public-argument forwarding (audit callbacks stay local)
and a narrow `file_issue` grant operation; callers cannot select projects or
gain generic create authority; each tool also returns bounded lexical suggestions and atomically records an explicit NEW/AMEND/DUPLICATE/SUB-ITEM/RELATED classification.
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
`product_currentness_get` is the default-off Semantic MCP projection of the code-owned Stateful
acceptance contract. Its live evidence adapter binds the verified request principal, selected
runtime/run, exact ordinary-tool schema and PostgreSQL prerequisites into one reconciliation basis.
The first diagnostic layer deliberately has no functional-proof reader and therefore cannot return
TRUE. The proof layer consumes only a private, sealed external qualification receipt bound to that
same principal/runtime/basis and exact MCP/admission/CAS/replay/currentness outcomes; invalid or
misbound receipts cannot prove TRUE. Its private emitter requires an existing mode-0600 key and
creates one immutable, durably published mode-0600 receipt; it never creates keys or replaces
receipts. The host-only `switchstand-stateful-qualifier` computes the exact authenticated
`tools/list` digest and, for one explicitly selected disposable WorkId, creates its immutable
attempt journal before proving denied admission, stale CAS, one applied operation, exact replay,
and current readback. It derives the receipt basis from the same PostgreSQL/runtime/principal
adapter, seals PASS only after those outcomes, and re-evaluates the installed receipt to TRUE.
The configured product-currentness runtime also exposes the qualifier's narrow SHA/run-ID readback;
any simultaneous certification identity must match it exactly.
MCP arguments never supply evidence or acceptance booleans.
Landing either layer is inert. Enabling the tool, producing a live qualification receipt, or relying
on TRUE remains separate activation evidence and authority.
`observability.py` owns request-local, redacted terminal timing records and active-request SQL
interval aggregation and appends a bounded, privacy-safe subset after the business result through
one non-retrying transaction. Failures leave coverage partial. Migration
`0024_mcp_operation_timings` stores no SQL, parameters, payloads, prompts, notes, tokens, bodies,
or principal identity.
`flow_report.py` owns a private read-only JSON snapshot for one exact WorkId. The report remains
partial: it reports canonical current state and relations plus observed target events from one
read-only, repeatable-read transaction. It projects only digest-validated failure/resolution rows
whose owner is that exact WorkId, without exposing their private prose, and digest-validates
activation revision chains correlated by their source-owned product/return-owner bindings.
Activation remains `PARTIAL` because that source has no revision timestamps. Exact WorkId timing is
stable-cohort service evidence, never task critical path. A separate additive
source-coverage projection uses only `KNOWN`, `PARTIAL`, or `UNKNOWN`; absent exact candidate,
test-run, or durable MCP-timing correlation stays `UNKNOWN`. Its concise format is a deterministic
projection of the same JSON facts and coverage reasons, not another correlation or inference layer.
Exact managed-run receipts also provide bounded raw process-shape counts and repeated identical
completed-validation groups compared by a private validated content digest. The digest and any
derived fingerprint stay private; output group identities reuse request IDs. Envelope identity
does not prove the
absence of external clearing changes, so the repeated-expensive-attempt advisory remains `UNKNOWN`.
Missing pickup distributions, explicit shape caps, or comparable fix durations likewise remain
`UNKNOWN`. Advisories are shadow-only and never change work or routing.
Aggregate learning fails closed until a sufficiently covered completed-trace cohort exists. Its
stable projection retains null sample count, coverage fraction, runtime/schema/environment cohort,
metrics, and delivery estimate rather than promoting single-WorkId or best-effort MCP timing into
cross-work evidence.
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
Managed task-run evidence is correlated only through the source-owned requester or execution
WorkId. The report bounds it to the latest 128 request/bind/terminal-result receipts, exposes no
objective, result contract, summary, or evidence references, and reports request-to-terminal-result
duration only when the canonical request/result digests, stored timestamps, and run/result
identities are internally consistent. A bounded recent window is explicitly `PARTIAL`.
Completed managed-run intervals join the same observed-wall union; requested or bound but
unterminated runs remain point/state evidence rather than inferred elapsed work. Mechanisms without
an exact WorkId correlation, including host-maintenance receipts, remain `UNKNOWN` rather than
acquiring an agent-authored stage label.

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
Ordinary creation accepts one admitted parent WorkId or canonical project ID. Native canonical
creates enforce a self-rooted independent item or exact parent-root inheritance, mailbox-derived
`SELF` ownership, and a complete action-or-wait state; the sole ownerless exception is an inert
parented Evidence child. Mailbox identity grants no creation or assignment authority. Ordinary
relation changes accept parent/dependency WorkIds or canonical project-membership add/remove;
provider project/section/assignee identifiers and placement moves stay behind trusted internal
and provider boundaries.
Every current tool is explicitly non-destructive and bounded rather than open-world. Current tools
are idempotent under their stable identity or transition contracts. Registration rejects a new
ordinary tool until that classification is extended.

`edge_maintenance.py` is the retained historical service-specific maintenance transaction for this
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
Asana client; retained mutation owners are supplied explicitly. When priority claims are enabled,
the managed surface derives the claim target and grant version from that trusted launch, exposes
the active claim read/write tools, and bounds priority context to the active WorkId plus its
launch-bound read-only references.

The canonical server's `launch-bound` profile exposes work, history, read-only preflight, and the
same fixed-category reporting tools. Its project IDs cross the managed boundary only as sealed
prepared-launch inputs and pass through the scrubbed executor and canonical Docker wrapper; a
missing category remains unavailable rather than reading a mutable fallback. An explicit
activation opt-in adds mutation, while priority claims additionally exposes the same active-work
priority adapter and bounded context projection as the full managed server. The compatibility
`build_context_server` function delegates to the canonical builder; it is not another server
implementation or process. Profiles share contracts and state, but their authority and inventories
are intentionally not interchangeable.

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
Managed Codex carries an always-on developer-instruction contract that recovers launch-bound work,
exact open obligations, and only its current role/phase package across re-entry and compaction; it
does not reuse Root Coordinator manifest, currentness, tracker, or compact-hook controls.
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

- `canonical_work.py`, `canonical_relations.py`, `priority_claims.py`, and `work_events.py` own the explicit, inert
  compact zero-Asana schema definitions and repositories for current rows, stable title/completion
  search pages, legacy task aliases,
  dependencies, parents, simple project placements, and event history. `canonical_event_reads.py`
  projects DB event storage through the existing public history/event contracts without runtime
  wiring. `canonical_work_runtime.py` projects canonical current rows and relations through the
  existing get, search, create, scalar-update, parent, and dependency contracts. Protected creates
  atomically persist the row, requested parent or project placement, and effect receipt; protected
  parent/dependency changes and canonical project-membership add/remove atomically persist the
  relation and receipt. `task_control.py` owns append-only owner checkpoints whose server-computed
  control basis excludes notes but includes reviewed routing and structural relations; missing or
  unreadable checkpoints remain `UNKNOWN`, and intended effect labels never grant authority.
  `work_hygiene.py` derives deterministic readiness from canonical work, that checkpoint API,
  reviews, messages, typed finding capture, and unresolved effects. It creates no persistence,
  scheduler, scanner, ownership registry, repair, or automatic work. The repository's ordinary
  edge constructs this runtime directly; deployment and cutover remain separate effects.
  Migration `0008` materializes the compact tables; migration `0014_canonical_routing` adds the
  nullable canonical-root, owner, and next-action routing projection without inferring legacy
  values, `0015_work_admission_time` adds nullable admission time, and `0017_priority_claims`
  adds inert, occurrence-based priority provenance without replacing the legacy scalar. Native PostgreSQL creates
  set it from the server clock; preexisting and source-imported rows retain explicit `NULL` unless
  the import carries a trustworthy timestamp. `work_policy.py` alone validates resultant root, owner, wait, lifecycle, and next-action
  state for semantic writes; legacy incomplete rows remain editable through title/notes-only
  changes. The repositories remain outside shared
  `state.metadata` and are registered explicitly by the edge. Migration
  `0025_task_control_checkpoints` adds only the durable checkpoint table; schema migration,
  service deployment, grant issuance, client refresh, and live reliance remain separate effects.
  Migration `0026_agent_message_ack` extends the existing delivery constraint with the
  `ACKNOWLEDGED` processed-notification state; it adds no table or store and refuses a downgrade
  while acknowledged rows exist.
  `SWITCHSTAND_TASK_CONTROL=1` is the explicit default-off service/grant selector; without it
  checkpoint and hygiene calls report unavailable and ordinary admission has no write operation.
- `priority_claim_service.py` owns an injectable, default-off MCP projection. Reads expose exact
  work/project claims and explicit source currentness. The ordinary authenticated workspace
  adapter derives `HUMAN_PRIORITY` for SET/CLEAR requests made while following explicit current
  Marco direction; its schema exposes no claim-kind, authority, source, grant, or qualification
  selector. WORK revisions fence a new write but are not stored as HUMAN source currentness, so a
  HUMAN claim remains current until superseded or cleared. Exact CLEAR inserts a non-current
  deterministic tombstone in the existing table for replay/history. Managed task-bound writes
  remain active-WorkId-only `AGENT_RECOMMENDATION`, retain stale-on-source-revision semantics, and
  reuse `effect_intents` for replay/conflict recovery. Neither surface can mint the other's kind.
  The production edge and managed task-bound server construct and register this projection only
  when `SWITCHSTAND_PRIORITY_CLAIMS=1`; the same switch enables the bounded read-only
  priority-context projection. The managed write adapter omits caller-supplied WorkId and grant version and derives both
  from the trusted launch. Activation remains a separate deployment effect and requires verifying
  the affected client-visible MCP schema.
- `priority_context.py` composes a deterministic read-only view for at most 50 explicit WorkIds
  from canonical work, relations, and priority claims. It keeps project claims contextual, labels
  agent claims advisory, exposes stale/conflicting/unknown evidence, and never parses notes or
  stores ranking, attention, or inherited claims. Its MCP/service seam is injectable and
  default-off; exact-scope completeness is not portfolio completeness. Work-level
  `priority_knowledge` derives solely from WORK claims: project BAND/BEFORE/HOLD, conflict,
  staleness, unknown currentness, and read failure remain visible context but never change a
  member WorkId's classification.
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
- `human_reviews.py` owns an inert exact-consequence/decision store. V2 consequences retain an
  immutable approved baseline: four-line Human Review opening, attributable sources, approved
  meaning, protected requirements, non-goals, decision context, named continuation and optional
  superseded baseline. The consequence digest is its baseline identity; approvals remain
  append-only. Legacy V1 exact-revision rows remain readable but cannot authorize cross-revision
  baseline conformance.
  `reviews.py` owns durable review requests and outcomes.
  Typed reviews route to the `Coordinator` mailbox. That reviewer endpoint is distinct from the
  `Codex Coordinator` control mailbox and does not follow its `/root` rename or takeover lifecycle.
  Codex: reviews = a fresh native fork; never the `review_*` tools.
  Its default-off non-Codex path is
  `review_request` -> `review_get` polling -> bounded focused rereview -> Human Review;
  pending responses explicitly direct sleep 20 seconds before every exact-ID poll.
  Requesters record the authorized post-PASS step in the subject's existing
  `review_next_action` before submission. Terminal `review_get` responses use that
  revision-checked instruction for PASS, name findings for disposition/focused resubmission,
  or direct evidence-based blocker recovery. Missing continuation or blocker evidence
  requires agent-owned record reconciliation, never an invented coordinator wake-up.
  Verdicts alone do not end the turn or grant effect authority; Human Review or an
  evidenced human-action blocker can end it, with real host limits preserved separately.
  `review_recover` can rebind only an exact received delivery after a same-principal mailbox
  takeover, and `review_submit` remains the sole verdict mutation. Requester and reviewer
  identities come from the current authenticated agent mailbox and are never public arguments.
  The occurrence row serializes authoritative request creation, so polling can acquire a newly
  available configured reviewer without a Coordinator mailbox or manual message choreography.
  Legacy typed occurrences remain readable when their exact stored basis and delivery pass current
  invariants. The resource service accepts one explicit default-off review policy/guidelines
  configuration and constructs one mailbox/occurrence graph shared with the independently enabled
  implementation-request facade, avoiding divergent review truth. Production configuration and
  activation remain separate. `review_config_loader.py` loads that configuration only when optional
  `SWITCHSTAND_REVIEW_CONFIG_PATH` names one absolute, bounded, mode-0600 closed/versioned JSON
  file; unset remains off, every present path is literal, and configured-invalid fails startup
  closed. The file carries policy and guideline identities plus an immutable partial reviewer map.
  The installed `typed-review-v1-cutoff` receipt continues to reject generic
  `review.request`, `review.acquisition`, and `review.outcome` message routes; only the typed review
  owner writes them. Unmapped kinds remain `WAITING_REVIEWER`. Client-visible activation requires
  a ChatGPT reinstall followed by fresh-chat schema and behavior verification.
  Baseline checks in the existing review basis bind the owner comparison to the exact candidate;
  reviewer assessments bind the baseline, phase, typed disposition, evidence and verdict digest.
  `implementation_requests.py` converts an approved V2 baseline plus an exact independent
  PRE_IMPLEMENTATION `BASELINE_PRESERVED` proof into an inert request. Its authorization reference
  binds baseline and review evidence, and its milestones derive only from named continuation.
  Missing/ambiguous baseline evidence is scoped to dispatch and creates no lifecycle hold. The
  default-off ordinary MCP adapter keeps baseline/review identities server-derived and never
  activates pickup or execution.
  `activation_continuity.py` and `activation_continuity_store.py` own one append-only obligation
  per installed product/revision/phase contract after technical activation. The server derives
  transition actors from current grants plus a current exact-WorkId runtime binding, accepts only
  server-resolved target/phase/contract-bound acceptance, adoption, and clearing proofs, rechecks
  exact product currentness before delivery, and enforces separate authorities. Its MCP transition and owner-local
  `work_get`/`work_update` projection exist only when the store and immutable contracts are
  injected. `activation_contract_loader.py` can load those contracts from one explicit bounded,
  closed, versioned mode-0600 JSON file into an immutable obligation-ID registry. The resource
  service accepts only an explicitly injected registry and otherwise leaves activation continuity
  unconstructed and absent from its tool inventory. Its host-only configured wrapper loads that
  registry only when optional `SWITCHSTAND_ACTIVATION_CONTRACTS_PATH` names one absolute private
  file; unset remains off and configured-invalid fails startup closed. Migration `0023_activation_continuity`
  persists revisions and refuses a downgrade that would discard any obligation. Landing remains
  inert: no public create, runtime launch, feature enablement, migration application, or activation
  is included. When both an activation-contract registry and product-currentness configuration are
  explicitly installed, the authenticated edge binds `ACTIVATED` to the caller's current grant and
  live MCP session and resolves technical truth through the exact installed contract plus Stateful
  currentness. Acceptance, adoption, and blocker clearing resolve only an exact caller-selected
  `task-run-result:<UUID>` through the digest-verified managed request/result store. The selected
  result must be the request's terminal result, come from a START validation requested by the
  contract's product owner, carry the exact installed contract in its result contract, match the
  requested transition outcome, and retain nonempty server-read evidence references; every
  mismatch remains fail-closed.
  `human_review_shell.py` provides a default-off server-rendered password-only and exact-Origin
  confirmation app over that store. Its local persistent factory wires the canonical state owner
  without owning the database engine lifecycle. Login issues a secure, HttpOnly, SameSite session
  lasting 30 minutes (invalidated by restart); the page offers only Approve and Reject and displays
  the saved decision. `human_review_runtime.py` adds an explicit,
  default-off command that owns the engine and Uvicorn lifecycle, requires every configuration
  value, and accepts only a loopback IP listener. `human_review_host.py` renders one create-new
  user-systemd service asset from explicit systemd-literal-safe real-executable runtime,
  mode-0600 environment, and port inputs while fixing the listener to loopback. It publishes only
  a fully written and fsynced same-directory staging inode without replacing an existing unit; it
  does not install, start, or enable a service. No public route or credentials are installed; no
  dispatch or activation is enabled. `human_review_caddy.py` renders a single inert route fragment
  only from an explicit literal public path ending in the shell's fixed `/human-review` path and
  an explicit loopback port; it does not select, install, or mutate a Caddy route.
  `human_review_propose.py` is a host-only command that accepts one bounded, closed consequence
  JSON from an exact mode-0600 regular file without following its final path component. It can only
  propose or replay that consequence; it cannot record a decision, dispatch work, or activate it.
  When `SWITCHSTAND_HUMAN_REVIEW_ORIGIN` is configured on the ordinary edge, `human_review_create`
  and `human_review_get` reuse the same store. Creation requires the current authenticated session's
  registered mailbox and binds its server-derived name and mailbox ID into the immutable digest;
  the returned URL identifies one exact WorkId/revision. These tools cannot decide requests. Legacy
  consequence digests remain unchanged. This is an API boundary: host agents running as the same
  OS user can still access database credentials and files.

  Setup remains explicit: a human provisions the bcrypt password hash privately (never through
  agent chat), then the shell environment needs `DATABASE_URL`, `SWITCHSTAND_HUMAN_REVIEW_ORIGIN`
  (exact HTTPS origin), `SWITCHSTAND_HUMAN_REVIEW_PASSWORD_HASH`, and loopback
  `SWITCHSTAND_HUMAN_REVIEW_BIND_HOST` / `SWITCHSTAND_HUMAN_REVIEW_BIND_PORT`. No username is used.
  Run the existing `switchstand-human-review` entry point behind the separately authorized HTTPS
  route generated by `switchstand-human-review-caddy`; configure the same origin on the MCP edge.
  No credentials, running service, or public route are provisioned by repository landing.
- `human_trajectory.py` owns an inert, append-only record of bounded human-direction continuity.
  Its `RECORDED_HUMAN_DIRECTION` provenance is not implementation authorization, it has no public
  MCP wiring, and landing its schema does not activate process reliance or provider cutover.
- `task_runs.py` owns inert INVESTIGATION, VALIDATION, and server-derived IMPLEMENTATION requests;
  its managed public request adapter remains INVESTIGATION/VALIDATION-only, with trusted
  START-to-RunReceipt bindings. The managed server constructs its persistence owner and registers
  request and result adapters, which admit only the exact managed principal with a current
  launch-bound `agent_task` grant and write authority for the active WorkId. Result submission
  accepts only an already-bound execution and server-derived runtime currentness; stale or unknown
  currentness preserves evidence nonterminal for later reconciliation. The managed launcher can
  bind one explicitly supplied exact START request to its durable RunReceipt before
  development setup; omission performs no selection or binding, and uncertain binding aborts launch.
  The managed client allowlist and default grant exclude `agent_task`. An explicit trusted
  `--agent-task` launch opt-in atomically adds that operation to the newly rotated launch grant and
  exposes only the existing request/result adapters to that launched client. It selects no request
  and starts no child. Continuation/takeover, request-selected or automatic launch,
  ordinary HTTP exposure, and activation remain absent.
- `grant_state.py` owns `work_grants` and `effect_intents`. `WorkGrant` in `grants.py` is the current
  caller authority contract; an operation ID identifies one protected effect across reconciliation.
- `messages.py` owns `messages`, `message_deliveries`, and the historical `message_projection`, including the
  AVAILABLE/RECEIVED/ACKNOWLEDGED/DISPOSITIONED lifecycle, result/effect evidence, and runtime-currentness
  checks. Message routes accept and create only the durable PostgreSQL message and delivery records;
  the historical projection table remains readable only for frozen-cutover disposition and later
  schema retirement.
- `agent_mailboxes.py` owns the temporary `agent_mailboxes` binding of a visible immutable agent
  name to an authenticated principal and hidden chat-session hash, with an independently generated
  endpoint UUID and generation. Endpoint UUIDs are message addresses, not WorkId identity; new
  endpoints are not inserted into `work_handles`, although pre-migration handle rows can remain as
  unreferenced legacy residue. Registration is read-or-create for that authenticated session, except
  that the canonical `codex-coordinator` name key and temporary `root` alias are reserved: an
  unbound session cannot create either through `agent_register`. A resume returns the session's
  existing mailbox rather than renaming or taking it over. Every cross-principal `agent_takeover`,
  including `Codex Coordinator`, stops with `takeover_confirmation_required` unless the request
  carries Marco's full current message naming the target and directing the takeover. The denial
  says that no takeover occurred and tells the agent to ask Marco and retry with his confirmation;
  no passcode is configured or accepted. A confirmed Coordinator takeover renames a legacy `root`
  row in place, preserves the endpoint and deliveries, replaces the principal/session binding, and
  increments its generation.
  `/root` resolves only as an inbound alias and successful readback uses `Codex Coordinator`.
  Other names retain the existing
  same-principal behavior. For older cross-principal recovery, the destination authenticated session
  records an exact preimage-bound request; the host-only `switchstand-agent-mailbox-transfer`
  command approves it in one transaction. It preserves the endpoint and deliveries, increments
  generation, fences the old principal/session, and fails closed if the mailbox or destination
  changed. Ordinary same-principal `agent_takeover` is replay-safe for the exact replacement session;
  its public MCP path retries one `state_unavailable` result with the captured unchanged identity and
  arguments and requires an actor-binding readback before returning success. Explicit denial or
  conflict is not retried. There is no public approval tool. `agent_messages.py` supplies
  public name-based views over `MessageState`; migration `0016_agent_mailbox_transfers` owns audit.
  Ordinary agent payloads are at most 800 characters in canonical compact JSON and pending pages
  are at most ten, newest first. Legacy oversized payloads remain server-side for identity checks
  but public pending/replay views expose only omission and character-count metadata.
- `lifecycle.py` owns `lifecycle_obligations`, the durable required-result continuation state.

The agent mailbox layer is current product behavior, but it is a compatibility bridge rather than
the final identity model. It reuses the UUID-shaped sender and recipient columns in `MessageState`
without asserting WorkId identity or requiring or creating provider bindings. Migrated installations
can retain the legacy handle residue described above. The HTTP edge reads exactly one hidden host
identity: ChatGPT `openai/session` unchanged, Codex `threadId` namespaced as `codex:<id>`, or, only
for the exact authenticated Claude Code OAuth client, a stable Claude namespace. The mailbox also
keys that namespace by the authenticated OAuth principal, so a registered Claude name survives
transport reinitialization without being shared across principals. Claude does not consume another
host's metadata namespace. Other missing, invalid, or ambiguous identities fail closed; the mailbox
stores the identity hash and binds one visible name per principal-and-chat pair.
Principal, chat identity, visible name, mailbox
generation, MCP session currentness, and WorkId remain distinct. It should be retired or reshaped
only after the canonical agent/work identity model and migration of outstanding mailbox state are
implemented and verified.

`agent_message_acknowledge` moves one to ten ordinary AVAILABLE/RECEIVED notifications to
ACKNOWLEDGED. ACK means only that the notification was processed: it does not reply, complete,
record a verdict/effect, or close a sender watch. Typed-review routes reject ACK. An exact result
may reply from ACK and atomically stores the result plus exact-evidence disposition; replay
converges both records. Recipient pending reads show only AVAILABLE/RECEIVED deliveries, while
open request watches and proposed-owner references include ACKNOWLEDGED until disposition.

Message receipt is fenced twice. The mailbox generation fences trusted principal takeover;
`RuntimeCurrentness` fences replacement MCP sessions or managed runs. Recovery is an explicit
transition. A new session or caller must not silently continue a RECEIVED delivery.

## Protected effects and UNKNOWN

The canonical runtime and `effects.py::CanonicalAppendGateway` own ordinary PostgreSQL work
mutations. A parented create normally inherits its parent's exact canonical root. The exact
imported `START HERE` `1218327002478382` rootless `Context` is a navigation-only placement
boundary for the documented direct-assignment `Task` shape: each such child remains parented
there but starts its own exact canonical root. Other parents and child shapes fail closed.
The following legacy gateways remain only for managed/source compatibility and the
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

The publisher also uploads the same bundle, manifest and checksum as an Actions ZIP artifact before Release publication. Its provenance marker binds a deterministic artifact name to the existing run identity and selected SHA. Resolution exposes an optional `actions_artifact` locator only for matching markers; it does not check retention or download bytes. No artifact enumeration, new service or credential expansion is introduced.


`repository_bundle.py` resolves the ordinary `repository_bundle_get` transport. The rolling GitHub
release is only a cache: it contains a bundle, manifest, and checksum published by
`.github/workflows/repository-bundle.yml` after Quality or an authorized exact-branch manual dispatch.
Each artifact contains exactly one branch with complete original history: `main` by default, or one
explicit branch. The publisher never fetches or bundles all branches; non-main asset names hash the
branch name. Resolution reads only that exact authoritative ref and requires a single matching
manifest ref, rejecting legacy all-head manifests. Any requested SHA must equal the selected head.
The manifest digest, release asset digest, and checksum must still agree. An unproved
grounding claim or cache transition returns `refresh_pending`; provider failure returns
`unavailable`. Those release and ref API reads use only the dedicated
server-side `SWITCHSTAND_REPOSITORY_BUNDLE_GITHUB_TOKEN`; missing configuration fails before network
access. The bearer is attached only to validated exact `https://api.github.com` bundle endpoints,
authenticated redirects are rejected, and release asset downloads
remain unauthenticated.

Pending responses name the selected branch and authorized-agent dispatch route; a read request does
not start publication. Release metadata is not source authority or downloaded bytes: consumers must
verify SHA-256, clone the bundle and inspect the requested Git history. Actual ChatGPT download and
materialization require client evidence; server tests do not establish that acceptance.

`repository_candidate.py` owns the ordinary read-only GitHub candidate qualification. It binds the
current public pull-request base and head to the merge-ref commit's ordered parents, then evaluates
the stable exact-head/composition terminal Quality evidence in its versioned code-owned catalogue.
Each terminal job is the trusted workflow's fail-closed aggregation of exact planning, the selected
or full Quality execution, and Docker when the reviewed policy requires it; its check name never
varies by internal mode. Missing, stale, ambiguous, skipped, or wrong-subject terminal evidence
fails closed; provider failure is `UNKNOWN`. Compact
gate identity, reason, and timing are returned by default, while bounded failed-step and check-output
detail is opt-in. This provisional preferred read neither authorizes nor performs review, merge,
ruleset, credential, provider-write, or rollout effects; raw public GitHub reads remain a diagnostic
fallback during qualification of the semantic path.

Selective foreground is default-off behind `SWITCHSTAND_SELECTIVE_FOREGROUND_ENABLED=true`; all
other values use the full suite. Adaptive broad truth remains shadow-only and preserves its backstop.
Quality always binds its exact subject and computes its validated plan under Python 3.14; it
fetches broad history only when a promoted class can still apply. Skipped health/adaptive
observations remain `UNKNOWN` and never reduce full-suite or Docker lifecycle requirements.
Ordinary PR plans use the verified execution composition, so consumers introduced by an advanced
base participate in selection. Alongside direct-test-only promotion, the closed affected-input
class permits `scripts/package-size.py` through its explicit subprocess consumer and
`coordinator_worker_state.py` through transitive Python consumers. Every changed input must be
accounted for; unknown inputs or missing explicit targets force full. Source/script selection
retains Docker lifecycle qualification; only direct-test-only selection omits it. Serial-sensitive
modules, cumulative stack tops, dependencies, migrations, CI infrastructure and nightly/main broad
backstops retain full execution. `repository_candidate.py` recognizes the actual selected shard-zero
job and records causal quality and successful lifecycle separately from broad-suite quality.
Full fallback balances pytest's actual collection into four whole-module shards using measured
costs in `scripts/pytest-shard-weights.json` and a fixed default for unmeasured modules.
`scripts/pytest-shard` assigns modules deterministically. Each runner owns a PostgreSQL service;
Ruff/Pyright run on the last shard, including the sole runner in selected execution. The stable terminal gate
requires matrix success and the existing lifecycle policy. Per-shard artifacts retain collected
and assigned node IDs and identify partial `full-shard` metrics rather than claiming a full suite.
`full_suite_due.py` owns its pure tri-state evaluator: known baseline absence or non-ancestry is
`FULL_SUITE_DUE`, while unavailable or contradictory observations are `UNKNOWN`. It neither
dispatches CI nor reduces the existing cadence/backstop, and it grants no merge authority.

`stacked_delivery.py` owns the pure fail-closed identity and evidence predicates for
GitHub-native stacked delivery. It can preserve a focused layer verdict only across an
unchanged layer diff and reviewed dependency/interface contract, applies proportional
inert/runtime proof, and requires fresh exact-top cumulative review and quality before
landing. `scripts/verify-quality-composition` binds an Actions checkout either to the
ordinary PR merge parents or to the exact native-stack first-parent composition chain.
Stack discovery, CI execution, review storage, and merge effects remain outside it.

## Launch, candidate, and host control

`launch.py` owns managed launch orchestration: linked-writer validation, exact-revision preflight,
development preparation, process supervision, and cleanup requests. `codex_runtime.py` owns Codex
command construction, configuration/readback, and managed runtime profile validation.
`run.py` supplies durable run identity/currentness checks.

For isolated launch, host-side `launch_source.py` resolves a WorkId or legacy task reference through
canonical PostgreSQL work and reads the protected source request from its notes;
`candidate.py::prepare_launch_source` verifies and materializes the exact Git repository/ref before
preflight. This is a transitional trusted-host bridge, not a provider API for agent code.

Ordinary Codex uses the official npm-installed CLI directly. Repository configuration and
instructions supply project behavior; Switchstand does not own, wrap, repair, or redirect the
global `codex` command. A stateless repository hook re-injects the existing Feedback V1 obligation
on every user prompt and immediately after compaction. It performs no classification, reporting,
network call, transcript scan, or persistence; the agent still uses the canonical Tracker Issues
V1-A tools and their readback. Its one-second local timeout preserves prompt acknowledgement as the
first response, and failures are visible and nonblocking.
The synchronous mailbox `Stop` hook calls the loopback-only
`/internal/codex-stop-mailbox` route on the existing edge. That route resolves only the current
`codex:<threadId>` session binding and returns read-only, newest-first pages of pending delivery IDs
without receiving or changing them. The hook follows continuation cursors, with
`stop_hook_active` as the mandatory continued-stop escape; missing, replaced, malformed, timed-out,
or unavailable state fails open visibly. The public host does not route this path. The optional read-only
`check_obligations` projection on that same route resolves the current `Codex Coordinator`
binding, explicitly enabled TEST-prefix mailboxes, or one explicitly opted-in head. It reads all unfinished
canonical assignments owned by that binding (`agent:<name_key>` for ordinary agents;
the existing Coordinator owner key is preserved). A private host opt-in at
`~/.config/switchstand/codex-stop-head.json` may additionally name one exact session,
registered mailbox name/generation for a directly assigned head. Its retained `assignments`
map is compatibility input, not an action whitelist or authority grant. Configuration and binding
are rechecked before returning actions; missing/malformed opt-in or stale evidence releases UNKNOWN.
The same file may set the explicit boolean `test_prefix: true` to enable TEST-prefix mailboxes;
removing that flag disables the prefix trial while preserving any exact-session opt-in.
Current executable owner actions and due review/message watches may reject a dedicated
HANDOFF, REAL_BLOCKER or ASSIGNMENT_COMPLETE Stop once; ordinary prose and conversation pass. Missing authority, stale or contradictory evidence
releases without certifying completion. With no runnable obligation or pending delivery, conversation passes. Both Stop handlers pass explicit
USER_STOP and honor `stop_hook_active` before any network check. They never mutate obligations or
certify recipient pickup, and the second Stop releases even if work remains. Completion, blocker
and handoff markers do not bypass the work check; ordinary prose is not a machine
completion certificate. This is not a scheduler or an autonomous wake mechanism.
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
For ordinary stock-Codex checkout freshness, `scripts/sync-canonical-main` is a separate narrow
host path: its user timer fetches and runs `git pull --ff-only` only when the fixed canonical
checkout is clean, on `main`, and strictly behind `origin/main`. Dirty, non-main, ahead, and
diverged checkouts are left unchanged. It does not launch or resume agents.
Legacy generation manifests and currentness controls remain temporarily readable for pre-cutover
sessions. Stock Codex does not create or consume them. They retire after every old generation and
pending handoff is drained.
The checker emits each required durable reread as a name-to-absolute-path mapping: every generation
rereads its exact start-commit record, and only an addressed successor manifest adds its validated
handoff obligations path. Current work remains the active assignment and WorkId; the compaction hook
does not invent a duplicate local current-work pointer. Missing, relative, or malformed live-read
paths make that mechanical re-grounding `CURRENTNESS_UNKNOWN`.
Pre-cutover generations may still have launcher-created writers, snapshots, and handoff receipts.
Their frozen compact/currentness checks and `switchstand-worktree-lifecycle` remain only to drain
and retire those exact generations. Stock Codex creates none of this state and does not use the
legacy launcher selectors, profiles, hooks, repair units, or dispatcher.

The Coordinator-control MCP also owns the narrow asynchronous implementation handoff. An exact
WorkId, caller-chosen operation identity, and bounded objective start the existing trusted
`switchstand --isolated` path, which binds a managed Worker to that WorkId and its durable
task-private linked writer. Spawn returns immediately so Root remains available for coordination;
status reports the exact base/head, branch, writer, commit summary, and terminal or UNKNOWN state.
A worker record binds the writer to the selected immutable CONTROL clone's exact Git common
directory, rather than assuming the independently cloned CONTROL shares canonical `main`'s Git
metadata. The host unit supplies the exact user-managed updater Codex executable through
`SWITCHSTAND_CODEX_BINARY`. For immutable older controls that still execute literal `codex`, the
same dedicated updater `bin` directory is the sole user-writable `PATH` prefix, followed by
`/usr/local/bin:/usr/bin:/bin`; newer controls use the exact binary variable. This bounded
compatibility exception avoids a stale CLI without adding broader user-writable lookup paths.
A deterministic host unit and durable phase record survive Coordinator restart; cancellation uses
the managed run receipt when present, stops only that unit, and retains the writer and logs as
recovery evidence. The seam grants no merge, deployment, or activation authority.

Built-in Workers share the Coordinator filesystem profile, so hooks/worktrees are concurrency guardrails, not a sandbox. Unbound Workers stay read-only; Root may bind one agent, WorkId, base and non-overlapping surface to a dedicated writer. The hook confines edits and admits only the existing helper's fixed test/commit actions; arbitrary shell remains denied. Root validates candidate identity, cleanliness and ancestry; stronger-containment work uses the isolated implementation Worker.

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
| Change production edge replacement sequencing | `edge_deploy.py` | deploy transaction tests and `chatgpt-mcp-edge.md`; the generated launcher binds product-currentness runtime/selected SHAs to the deployed candidate; keep landing inert and activation separate |
| Change shared private byte-file durability mechanics | `secure_file.py` | edge maintenance and rehearsal callers; keep schemas, state machines, path policy, and domain errors local |
| Change first combined-to-split activation sequencing | `stable_auth_deployment.py` | stable-auth deployment/migration/host tests and `chatgpt-mcp-edge.md`; preserve the public gate and never rewind possibly-written OAuth state |
| Change inert Wakeful event persistence | `wakeful.py` | `wakeful.md`; keep probe and agent adapters outside the neutral contract |
| Change inert edge-monitor classification | `edge_monitor.py` | `wakeful-edge-monitor.md`; do not add host activation or agent dispatch here |
| Change inert edge-monitor host qualification | `edge_monitor_host.py` | `wakeful-edge-monitor.md`; keep scheduling and delivery outside it |
| Change provider-neutral work discovery or WorkId binding | `discovery.py`, `state.py` | `chatgpt.py`, provider search/structure implementation, migrations when storage changes |
| Change the inert compact zero-Asana work/relations/event model | `canonical_work.py`, `canonical_relations.py`, `priority_claims.py`, `work_events.py`, `canonical_event_reads.py`, `canonical_work_runtime.py` | explicit canonical metadata, migrations `0008`, `0014_canonical_routing`, `0015_work_admission_time`, and `0017_priority_claims`, real-PostgreSQL repository tests, public projection tests, default-off service wiring, and protected create/update/relation atomicity |
| Change recorded human-direction continuity | `human_trajectory.py` | migration `0009` and trajectory tests; preserve append-only provenance without turning it into implementation authority |
| Change private flow evidence reporting | `flow_report.py` | exact WorkId database correlation, bounded digest-validated failure/activation reads, and source coverage; no external inference, mutation, or process control |
| Change Asana payload or relation semantics | `provider.py::AsanaProvider` | `relations.py` when the provider-neutral contract changes, plus provider and gateway tests |
| Change protected-effect recovery or UNKNOWN behavior | the relevant gateway and `grant_state.py` | provider readback implementation and causal ambiguity/retry tests |
| Change message lifecycle/currentness | `messages.py` | managed and ordinary adapters; `agent_mailboxes.py` for name/principal/generation binding changes |
| Change managed launch or runtime behavior | `launch.py`, `codex_runtime.py`, `run.py` | `candidate.py`, `launch_source.py`, development lifecycle, and launch/runtime tests according to the boundary touched |
| Change repository bundle freshness or publication | `repository_bundle.py`, `.github/workflows/repository-bundle.yml` | `repository_bundle_get`, bundle resolver tests, and bootstrap guidance |
| Change development workloads or cleanup | `development.py` | `docker.py`, development MCP scripts, and real-Docker qualification when the claim crosses that boundary |
| Change copied-state rehearsal provisioning | `rehearsal_target.py` | `development.md`, zero-Asana qualification, and disposable Docker ownership tests |
| Change resource-worker admission, execution, or live proof | `agent_broker.py`, `agent_executor.py`, `agent_canary.py` | [resource-worker architecture](resource-governed-agent-workers.md), owner-local tests, host pressure/sandbox boundaries, and the separate activation decision |
| Change affected-test selection or repository qualification | `affected_tests.py`, `repository_candidate.py` | their owner-local tests, `scripts/check`, GitHub Quality/composition evidence, and exact-head-versus-local truth |
| Change global raw-Claude routing or installation | `scripts/claude-shim`, `scripts/install-claude-shim`, `scripts/claude-dispatch`, `.claude/coordinator-*.json` | `tests/test_claude_shim.py` and the Coordinator hook tests |
| Change guarded canonical-main synchronization | `coordinator_sync.py` | stdio boundary tests and retained legacy `scripts/coordinator-handoff` interaction |

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

`wakeful.py` remains the neutral SQLite/outbox owner.
`disk_pressure.py` is a non-destructive, one-shot Wakeful producer that runs outside the Codex
launch path. It performs only a constant-time filesystem capacity probe, records an atomic private
SQLite transition, and emits only deduplicated pressure events. A separate user timer may schedule it.
It does not start Codex or deliver into Codex sessions. Stock Codex consumes durable mailbox
messages only while the user is actively running and prompting that session.
