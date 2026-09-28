# Agent-project bootstrap and ordinary MCP

## Current decision

Exposing durable-agent project bootstrap as an ordinary MCP write is **NOT READY** and is not proportionate now. The existing `switchstand-bootstrap-agent-project` CLI remains the operator route. It has a dry run by default and requires an explicit `--apply` for writes, but its use still requires separately established authority and careful operator supervision.

This decision does not authorize a production Asana bootstrap execution. Landing this document is inert: deployment, activation, granting access, and using any future write surface are separate effects requiring their own authority and evidence.

The practical need is occasional creation of one marked Asana project, its ordered `CURRENT` / `WAITING` / `DEFERRED` sections, four known custom fields, and one marked master task multi-homed into Main. That need does not justify a generic administration framework or exposing arbitrary Asana project, section, field, task, or membership mutation.

## Why the CLI cannot simply be wrapped

The CLI is a trusted operator program, not a durable protected-effect gateway. Its caller supplies raw workspace, team, project, and custom-field GIDs. It preflights and reads back the intended shape, but its progress and `wrote` flag are process-local. A transport failure after any one of its multiple provider writes can therefore leave an aggregate `UNKNOWN` outcome which an operator must inspect before rerunning.

A thin MCP wrapper would preserve those properties while adding a remotely callable write path. In particular, it would:

- let model arguments select provider identities and configuration;
- lack server-owned admission tied to a current authorized bootstrap profile revision;
- lack a durable OperationId fingerprint and replay/conflict contract;
- collapse several independently ambiguous writes into one process-level failure;
- offer no restart-safe, per-step reconciliation before another possible send; and
- risk treating the CLI's final JSON as a durable receipt even though it is not a persisted effect record.

Wrapping subprocess invocation or translating CLI flags into an MCP schema would not repair those boundaries. The sound future seam is a separate, deliberately narrow admin/operator operation which may reuse the CLI's validated domain rules but not its argument surface or process-local recovery model.

## Smallest sound future write surface

If the need becomes frequent enough to justify implementation, add one bootstrap operation to a separate admin/operator schema. It must not be added to the ordinary work-agent schema or generalized into arbitrary provider administration.

The complete caller-supplied request schema should be:

```json
{
  "api_version": "1",
  "operation_id": "UUID",
  "profile_id": "opaque server identifier",
  "profile_revision": 7
}
```

The server-owned profile resolves the role, project name, workspace, team, Main project, four custom fields, marker, section order, and any other provider identifiers. Every provider identifier is allowlisted in that exact profile revision; none is accepted from the MCP caller. Profiles are provisioned and revised only through a trusted host/operator path outside MCP.

Admission and authority are default-off. The operation is absent or denies before provider access unless the authenticated principal, deployment environment, operation, profile ID, and exact current profile revision are explicitly enabled by trusted configuration. Project membership, message content, possession of provider IDs, access to an ordinary work grant, or the ability to call other MCP tools grants no bootstrap authority. Revocation or a profile revision change prevents new admission and does not erase an already recorded uncertain operation.

Before the first possible send, persist the OperationId with a canonical fingerprint of `api_version`, operation name, authenticated principal/authority generation, `profile_id`, `profile_revision`, and the fully resolved server-owned profile. Define one canonical serialization (including stable field ordering and explicit representation of every resolved value) and hash that byte sequence. Reusing the OperationId with the same fingerprint returns or continues the recorded operation; using it with any different fingerprint is an identity conflict and sends nothing. Admission checks and creation of the initial durable record must serialize so two concurrent calls cannot both begin the operation.

Treat bootstrap as a compound operation with a fixed, server-owned step plan. Persist each step's intent before its possible provider write and its authoritative readback afterward. A transport error, cancellation, restart, malformed success response, or failed receipt persistence after a possible send leaves that step durably `UNKNOWN`. On retry, reconcile the same OperationId and step against authoritative provider state before deciding that the step is already applied or safe to send. Do not skip an unresolved step, restart the whole operation under a new ID, or let later steps imply that an earlier ambiguous effect did not occur.

Return one closed structured result with a finite top-level status such as `applied`, `in_progress`, `denied`, `stale`, or `unknown`; the same OperationId and profile revision; per-step state; exact verified postcondition; receipt; retry mode; and next action. `applied` requires authoritative final readback of the complete project shape and durable storage of that result. Unknown or unrecognized provider data fails closed rather than appearing in an open-ended success payload.

One product choice remains deliberately unresolved: whether the receipt should use provider-neutral project/task handles or expose exact raw Asana project/task GIDs to this trusted operator client. Provider-neutral receipts preserve the normal Switchstand boundary and make later provider change possible. Raw IDs are simpler for immediate operator investigation and match the current CLI, but extend provider-specific identity into the new surface. No implementation should silently choose between them.

## Reuse boundary and implementation envelope

Reuse the current bootstrap invariants: marker, three-section order, four custom-field compatibility rules, exact-project/master identity checks, preflight compatibility checks, and authoritative final shape verification. Reuse the established Switchstand protected-effect concepts where their contracts fit: authenticated principal resolution, default-off admission, durable OperationId conflict detection, possible-send-before-write persistence, work serialization, UNKNOWN preservation, and exact readback receipts.

New mechanism is still required for server-owned versioned bootstrap profiles, a compound-operation journal with per-step intent/outcome, restart reconciliation for each provider mutation, and the closed operator result. Existing single-effect work append/create machinery and the CLI's process-local `wrote` flag do not provide those semantics. Do not broaden either into a universal workflow engine.

A realistic initial implementation and test-support envelope is **700–1,100 gross changed lines**, measured as additions plus deletions against its immutable package base. This is a forecast alarm, not permission or a target. Crossing it, introducing arbitrary admin primitives, adding a general workflow/recovery framework, or requiring a second profile/provider authority system triggers replan before further expansion.

## Strongest smaller alternative

The strongest smaller next step is a read-only preview operation. Given only `api_version`, `profile_id`, and `profile_revision`, it would resolve an allowlisted server profile, run the current preflight/state inspection without writes, and return a closed plan describing existing versus missing project parts and the exact operator CLI action required. It should not accept an OperationId because it performs no protected effect.

This preview would remove model-supplied GIDs and catch incompatible or ambiguous state while keeping all writes on the existing operator route. It would not provide unattended execution, durable write replay, or recovery from an ambiguous CLI run. If those deferred guarantees are not materially needed, preview is the preferred endpoint and the write design should remain unimplemented.

## Evidence and activation

An implementation is not ready on unit tests or fake-provider choreography alone. Landing evidence must cover closed schemas, default-off admission, wrong principal/profile/revision denial before provider access, canonical fingerprint replay and conflict, concurrent duplicate admission, durable intent before every possible send, cancellation/restart at every step boundary, ambiguous provider responses, failed receipt persistence, and full final readback. Database-backed tests must prove records and UNKNOWN survive process restart.

Before any production reliance, qualify the exact reviewed candidate against a separately authorized disposable Asana workspace/profile. Inject or reproduce an ambiguous outcome at each mutation class where safely practical, restart, reconcile without a duplicate, and verify the exact final project, sections, fields, master task, memberships, journal, and receipt. Record the exact code revision, profile revision, provider objects, OperationId, step outcomes, and readback evidence.

Code landing, service deployment, schema/profile provisioning, tool registration, admission enablement, and an actual bootstrap call are separate states and effects. Each requires its own current authority and readback. In particular, neither this design nor a later inert implementation authorizes production Asana bootstrap execution.

## Decisions required before implementation

Implementation must not start until these seven decisions are explicit:

1. **Surface and caller:** the exact separate admin/operator server or inventory, authenticated principal class, tool name, and confirmation that ordinary work agents cannot call it.
2. **Admission authority:** who may enable, revoke, and inspect bootstrap authority; the authority-generation/currentness contract; and which deployment environments remain default-off.
3. **Profile ownership:** the trusted profile store and owner, revision/update procedure, exact allowlisted fields and provider IDs, and behavior when a profile changes during an operation.
4. **Operation identity:** the canonical serialization and hash algorithm, fingerprint fields, uniqueness/locking boundary, replay result, and identity-conflict result.
5. **Step and recovery contract:** the fixed step order, durable states and transactions, authoritative reconciliation query for every possible send, and terminal versus retryable UNKNOWN rules.
6. **Receipt vocabulary:** provider-neutral handles or raw Asana IDs, the exact closed result/readback schema, retention, and the operator evidence needed to investigate UNKNOWN.
7. **Delivery and reliance gates:** package base and approved limits, disposable qualification fixture and fault cases, reviewer/CI requirements, deployment/profile-provisioning owner, activation authority, and production rollback or disable procedure.
