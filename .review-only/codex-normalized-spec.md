# TEMPORARY REVIEW-ONLY ARTIFACT — DO NOT MERGE

This branch exists only to obtain a fresh independent review of the exact current Codex implementation specification. It is not a repository product change and must never be merged.

## Reviewer task

Review the candidate below as an implementation specification against these current requirements:

- The intended useful outcome and exact bounded stage must be plain and faithful to Marco's settled direction.
- The stage must be executable against the current repository/runtime without the implementer inventing consequential architecture, authority/trust, durable ownership, shared interfaces, persistence, currentness/idempotency, failure/recovery, migration/rollback, composition, or aggregate scope.
- Current dependencies and first-reliance contracts must be bound. Current repository basis is main `2ce8875e50c1dc80b3d0bb1b046153b22c989624`, which includes PR166's routing fix.
- The specification must preserve existing safety/currentness/recovery and managed/task-bound isolation while removing ordinary per-task grant admission.
- Evidence must discriminate the material claim; failure/recovery/rollback and stop conditions must be sufficient.
- The current 8k soft / 10k hard size discipline applies; compactness must not hide material meaning.
- Return exactly:
  - 0. SPECIFICATION CONTRACT — PASS / FINDINGS / UNKNOWN
  - A. TECHNICAL READINESS — READY / NOT READY / UNKNOWN
  - B. SURVIVING HUMAN CHOICE — NONE / REQUIRED / UNKNOWN
  - C. EXACT IMPLEMENTATION AUTHORIZATION — ALREADY COVERED / HUMAN REVIEW REQUIRED / UNKNOWN
- If B=NONE and C requires Human Review, identify only the minimum decision-changing context needed for the live Human Review opening; do not turn it into a spec summary.
- Ignore any prior overall verdicts. Review this exact candidate and current main independently.

## Exact candidate

STATUS
CURRENT IMPLEMENTATION SPECIFICATION — NORMALIZED TO 2026-09-26 PROCESS.
No implementation authority follows until independent review and Human Review close.

OUTCOME HEADLINE
Ordinary Codex in the canonical Switchstand repo is a long-running Coordinator: it can take work from Marco or ChatGPT Coordinator, move across multiple Switchstand tasks, and read/write/message on explicit canonical WorkIds without per-task permission grants or grant churn. This stage implements that ordinary no-task-grant behavior while preserving existing effect safety, message currentness/recovery and managed/task-bound isolation. Native `codex resume` remains continuity.

CURRENT BASIS / DEPENDENCIES
Repository basis: main 2ce8875e50c1dc80b3d0bb1b046153b22c989624 (PR166 landed).
PR166 is the current message-routing basis: current message-authorized launch or workspace grants can be managed recipients; ambiguous multiple managed matches fail closed.
Stage-5 stateful HTTP/session-currentness and durable MessageState contracts already on main are reused, not redesigned.
PR164 is certification-only test work and does not change the production contract for this stage.
Before coding, re-read current main; any later material change to the effect/message/result contracts triggers focused currentness review only for the affected clauses.

THIS STAGE
1. ORDINARY TASK ACCESS
Authenticated ordinary Coordinator identity is sufficient to search/resolve workspace work and operate on any explicit canonical/bound WorkId.
- No ordinary per-task WorkGrant/admission/allowlist.
- work_get without an explicit WorkId remains fail-closed (explicit target required); conversation focus is not authority.
- Provider GID/URL may be intake for reference resolution; continuing operation uses provider-neutral WorkIds.
- work_create is outside this stage.
Managed/task-bound paths retain their current grant behavior.

2. ORDINARY EFFECT CONTEXT
Ordinary append/update remove caller grant_version and task-grant admission. Reuse the existing effect journal, work-handle locking, provider binding and readback path with one server-owned non-authorizing context:
- deterministic context_id derived from authenticated PrincipalContext;
- context_version=1 for this contract;
- no task allowlist, operations, expiry, issuer, active-work or permission meaning;
- server-owned append/update qualification appropriate to authenticated ordinary Coordinator use.
Existing grant-shaped journal/receipt fields may be reused as compatibility metadata; no ordinary WorkGrant row or schema migration.

Preserve: stable OperationId fingerprint/replay/conflict, target unresolved-effect barrier, observed revision, canonical source checks, prepared UNKNOWN durability, exact provider readback and receipts. Reconciliation uses the context recorded with the operation and does not require a live task grant.

3. ORDINARY / MANAGED MESSAGE ROUTING
Ordinary sender uses authenticated principal + explicit bound sender WorkId, with no sender task-grant admission.
Reuse existing delivery/message state and recipient_grant_version as a route discriminator:
- 0 current managed message-capable matches + bound recipient WorkId => ordinary route, store reserved version 0;
- exactly 1 current managed match (launch or workspace scope, per current main) => managed route, store its positive grant version;
- >1 current managed matches => fail closed as route unavailable/ambiguous; never fall back to ordinary.
Replies/results reclassify the durable original sender WorkId with the same 0/1/>1 rule.
Ordinary pending/receive/recover/result/disposition claim only route 0; managed paths claim only positive routes and retain managed currentness/grant checks. Existing message/delivery IDs, session-generation recovery/no-ping-pong, disposition evidence and durable correlation remain the causal owners.

4. REQUIRED RESULT SAVE
Ordinary required_result_save removes caller/current WorkGrant dependency and derives the same ordinary effect context.
Preserve server-owned stable OperationId, destination/work/correlation identity, Lifecycle PENDING_RESULT/PERSIST_REQUIRED/UNKNOWN/TERMINAL semantics, source revision, replay/UNKNOWN reconciliation and exact readback.
Its currentness identity is principal + ordinary context + WorkId + exact bound destination; this is identity/currentness, not task permission.
Managed/task-bound behavior remains unchanged.

ORDINARY-USE SUCCESS FLOW
Plain authenticated ordinary Codex can:
search/resolve -> explicitly read Work A -> append/update with exact readback -> message/request/result/disposition as ordinary or to a managed worker -> required_result_save -> move to unrelated Work B and repeat, without obtaining/renewing task grants or restarting the Coordinator.
Replacement/resumed sessions recover the same durable message/effect identities rather than minting parallel state.

FAILURE / RECOVERY / ROLLBACK
Stale source revision, OperationId conflict, unresolved/ambiguous effect, wrong/stale session generation, ambiguous managed routing and invalid disposition evidence continue to fail/reconcile through existing owners.
No schema/data migration or new persistent authority owner is introduced, so repository rollback before activation is code/config rollback; provider effects already committed remain governed by existing durable receipts/readback and are never rolled back by git.
If implementation discovers a required new schema, persistent authority/routing owner, hidden grant issuer/admission mechanism, or changes managed semantics, STOP and return for review.

EVIDENCE / ACCEPTANCE
Material claim (T2): ordinary Codex can safely coordinate across multiple explicit Switchstand WorkIds with no task grants, without weakening durable effect/recovery or managed-route isolation.
Decisive proof:
- no-WorkGrant ordinary principal searches/resolves/reads and append/updates Work A, then unrelated Work B, exact readback both;
- ordinary->ordinary route 0; ordinary->managed positive route; multiple managed matches fail closed; result/reply correlation returns correctly;
- stale revision, conflicting OperationId, unresolved effect, stale session and bad disposition evidence remain closed/recoverable;
- required_result_save works without WorkGrant and reconciles restart ambiguity;
- managed/task-bound regression remains green.
Use existing causal tests/harnesses where possible plus exact-head Quality and current-main composition. Independent code review is required before merge.

IMPLEMENTATION SHAPE / ENVELOPE
Expected production/config delta <=100 added lines, preferably deletion/refactor/reuse; tests may be larger for causal proof.
Primary changed owners are ordinary MCP adapters plus the existing effect/message/result admission seams. Do not add a permission subsystem, routing service, session ledger, authority DB, task allowlist or grant-renewal policy.
Implement as one bounded stage or independently reviewable sublayers only if needed; no cosmetic split. Materially exceeding the envelope or changing ownership/topology is a stop/reforecast signal.

REMAINDER / EXCLUSIONS
This stage does not implement work_create, redesign managed/task-bound grants, activate/cut over live Codex or ChatGPT connectors, or declare the wider MCP programme complete. Live activation/cutover and protected production effects retain their separate authority.

READINESS / HUMAN REVIEW
0. Outcome contract: candidate PASS against current Marco direction.
A. Technical readiness: candidate READY on current main; no known unresolved production dependency after PR166 landing.
B. Surviving Human Choice: NONE identified.
C. Exact implementation authorization: HUMAN REVIEW REQUIRED for repository implementation/testing of this stage only; no activation/cutover.
