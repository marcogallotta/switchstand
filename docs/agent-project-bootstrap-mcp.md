# Agent-project bootstrap and ordinary MCP

## Current decision

ChatGPT has shell access, but that shell has no authenticated network path to Asana. The existing `switchstand-bootstrap-agent-project` CLI therefore cannot perform a ChatGPT-requested bootstrap from that environment. A read-only MCP preview followed by an operator CLI command can assist an operator, but it does not deliver ChatGPT-performed bootstrap.

The smallest credible solution is a deliberately narrow, default-off MCP bootstrap operation backed by one server-owned, immutable profile revision. It is proportionate to the practical need because MCP is the authenticated Asana network/effect bridge available to ChatGPT. It must not become a generic administration framework or expose arbitrary project, section, field, task, membership, profile, or provider-identity mutation.

This decision does not authorize a production Asana bootstrap execution. Landing code or documentation is inert. Deployment, profile provisioning, tool registration and enablement, fresh ChatGPT tool discovery, and any real Asana call are separate effects requiring their own authority and evidence.

The practical need is occasional creation of one marked Asana project, its ordered `CURRENT` / `WAITING` / `DEFERRED` sections, four known custom fields, and one marked master task multi-homed into Main.

## Why the CLI cannot simply be wrapped

The CLI remains useful domain code and an operator route only on a host that already has authorized Asana network access and credentials. Its caller supplies raw workspace, team, project, and custom-field GIDs. It preflights and reads back the intended shape, but its progress and `wrote` flag are process-local. A transport failure after any one of its multiple provider writes can therefore leave an aggregate `UNKNOWN` outcome which an operator must inspect before rerunning.

A subprocess wrapper or MCP schema that translates the CLI's flags would preserve those properties while adding a remotely callable write path. In particular, it would:

- let model arguments select provider identities and configuration;
- lack server-owned admission tied to the one trusted bootstrap profile revision;
- lack a durable OperationId fingerprint and replay/conflict contract;
- collapse several independently ambiguous writes into one process-level failure; and
- risk treating the CLI's final JSON as a durable receipt even though it is not a persisted effect record.

The MCP implementation should instead extract and reuse callable validation, preflight, convergence, and final-verification logic from the CLI's domain owner. It must supply its own trusted request boundary, durable effect record, serialization, and ambiguity behavior rather than invoking the raw CLI argument surface.

## Smallest credible MCP surface

Expose at most two bootstrap-specific tools on the authenticated HTTP/OAuth MCP edge:

- A read-only preview may inspect the current provider shape for the configured profile and return a closed plan. It is preparation and diagnosis, not completion of the bootstrap.
- A narrow apply operation accepts only `api_version` and a caller-generated `operation_id`. The caller cannot supply workspace, team, project, field, task, membership, profile, or other provider GIDs or names.

The server resolves one fixed, immutable bootstrap profile revision containing the role, project name, workspace, team, Main project, four custom fields, marker, and section order. The tools are absent or deny before provider access unless trusted configuration enables the exact authenticated principal, deployment environment, operation, and profile revision. Project membership, message content, knowledge of provider IDs, an ordinary work grant, shell access, or access to other MCP tools grants no bootstrap authority.

Before the first possible send, atomically persist the OperationId and a canonical fingerprint of the operation name, authenticated principal/admission generation, and fully resolved profile revision. Serialize all operations for that profile. Reusing an OperationId with the same fingerprint returns or reconciles its durable record; using it with any different fingerprint is an identity conflict and sends nothing. A different operation cannot start while that profile has an unresolved outcome.

The reduced first slice has deliberately conservative recovery semantics:

1. On a first admitted call, inspect authoritative Asana state, persist intent, then run the existing monotonic convergence logic and exact final readback.
2. A possible-send failure, cancellation, restart, malformed response, or failed receipt persistence leaves the operation durably `UNKNOWN`.
3. On replay, inspect authoritative state before considering any send. If the complete exact postcondition is visible, persist and return `APPLIED`.
4. If state is absent, partial, incompatible, or otherwise cannot prove the exact postcondition after ambiguity, remain `UNKNOWN`, send nothing, and block a new operation for that profile pending trusted manual adjudication. Absence at one read does not prove that a delayed provider write cannot still complete.

Return a closed result with a finite status such as `applied`, `denied`, `stale`, `not_applied`, or `unknown`; the OperationId and fixed profile revision; the exact verified postcondition when available; a receipt; retry mode; and next action. Unknown or unrecognized provider data fails closed rather than appearing in an open-ended success payload.

This reduced contract does not promise unattended continuation after an ambiguous partial bootstrap. Adding safe automatic per-step continuation would require persisted per-mutation intent/outcome, mutation-specific reconciliation, and a proven resend rule for every possible provider write. That is a materially larger design and remains deferred unless the product actually requires it.

## Reuse boundary and implementation envelope

Reuse the current bootstrap invariants and callable domain logic: marker, three-section order, four custom-field compatibility rules, exact project/master identity checks, preflight compatibility checks, monotonic desired-state convergence, and authoritative final shape verification. Reuse established protected-effect concepts where their contracts fit: authenticated principal resolution, default-off admission, durable OperationId conflict detection, intent-before-send persistence, profile serialization, `UNKNOWN` preservation, and exact readback receipts.

New mechanism is still required for the fixed server-owned profile, bootstrap-specific durable operation record, admission, serialization, replay inspection, and closed result. Do not force this operation through WorkId/grant semantics, broaden existing work effects into a universal workflow engine, or add profile CRUD to the model-facing surface.

A realistic initial implementation and test-support envelope for the reduced stop-on-`UNKNOWN` contract is **550–710 gross changed lines**, measured as additions plus deletions against its immutable package base. A design that promises unattended per-step recovery remains approximately **700–1,100 gross changed lines**. These are forecast alarms, not permission or targets. Crossing the selected envelope, introducing arbitrary admin primitives, adding a general workflow/recovery framework, or requiring a second profile/provider authority system triggers replan before further expansion.

The strongest smaller alternative is preview-only MCP plus the existing operator CLI on an authorized networked host. It removes model-supplied GIDs and catches incompatible state, but it does not satisfy ChatGPT-performed bootstrap and must not be described as doing so. It is appropriate only if operator execution is the accepted product outcome.

## Evidence and activation

Landing evidence for the reduced design must cover closed schemas, rejection of caller-supplied provider configuration, default-off admission, wrong principal/profile-revision denial before provider access, canonical fingerprint replay and conflict, concurrent duplicate admission, profile serialization, durable intent before possible send, and persistence of `UNKNOWN` across restart. Fault injection must show that replay after every mutation class reads first and sends nothing unless the record has never crossed a possible-send boundary; complete final state may promote to `APPLIED`, while absent or partial state after ambiguity remains `UNKNOWN` and blocks new work. Exact final readback must prove the complete project shape.

Unit and fake-provider evidence cannot establish the live authenticated provider boundary. Before reliance, qualify the exact reviewed candidate against a separately authorized disposable Asana workspace and fixed profile, including restart and ambiguous-outcome cases. Record the exact code revision, profile revision, provider objects, OperationId, durable outcome, and readback evidence.

Code landing, service deployment, profile provisioning, tool registration/allowlisting, admission enablement, fresh ChatGPT-session discovery, and an actual bootstrap call are separate states and effects. Each requires current authority and readback. In particular, neither this design nor a later inert implementation authorizes production Asana bootstrap execution.

## Decisions required before implementation or activation

The material implementation decision is whether the first slice may stop at durable `UNKNOWN` for trusted manual adjudication after an ambiguous partial bootstrap, or must provide unattended per-step continuation and recovery. That choice selects the reduced or larger recovery contract and evidence envelope.

Before implementation, also bind the exact authenticated principal and MCP inventory/tool names; the fixed profile owner, contents, revision and admission generation; and whether the trusted receipt uses provider-neutral handles or exact Asana project/task GIDs.

Before activation, separately bind the deployment and profile-provisioning owner, disposable qualification target, enable/disable authority, fresh ChatGPT discovery proof, and exact authorized Asana target and call. Production bootstrap remains unauthorized until that final effect is explicitly granted.
