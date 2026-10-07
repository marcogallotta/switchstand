# MCP work, history, messaging, and source compatibility

Switchstand's current MCP model is provider-neutral and WorkId-based. Ordinary
clients discover or resolve admitted work, then use work, structure, history,
event, and protected-effect operations. Managed Codex receives its
active and reference WorkIds from the trusted launcher; those bindings, not tool
arguments or readable provider records, define its work boundary.

## Current work and history

Call `work_get(api_version="1")` for the launch-bound assignment, or pass an
admitted WorkId where the ordinary tool schema permits it. `item.id` is the
opaque WorkId. Its notes are the canonical ordinary current state. Use
`work_update(notes=...)` for current intent, progress, findings, verdicts, and
final/current results; update the controlling meaning rather than appending a
chronology.

History and exact-event reads are exceptional. They require an explicit
`investigation`, `recovery`, or `legacy_reconciliation` purpose and are not for
normal grounding, re-entry, current-work discovery, or routine polling. Use the
revision returned by `work_get` when reading bounded history or exact events. A
stale result means reread current work and restart the affected
paginated read; partial, malformed, unavailable, or stale pages never prove
complete history. Promote any controlling conclusion from exceptional history
back into notes before relying on it as current state.

Ordinary workspace clients can use provider-neutral search and exact legacy
reference resolution where their current admission permits it. Resolution maps
an already identified provider reference to admitted work; it does not make a
provider task ID a WorkId or grant authority. Structure and search results are
discovery evidence, not reassignment, approval, or permission to write.

Before relying on material mutable evidence, reread its exact current work or
event representation. Readability does not imply authority, and status labels
do not establish that an external effect occurred.

## Durable messaging and feedback

Durable message tools are the current agent-to-agent messaging surface.
Ordinary interactive agents use only the `agent_message_*` family and address
an immutable registered agent name. Managed task-bound runtimes retain the
`message_*` family for admitted WorkId routes and the managed review bridge.
Both use the same durable message state and preserve pending, receive/recover,
correlated result, and disposition semantics. Delivery, receipt, disposition,
effect, and completion are distinct states.

When an authorized replacement session gets `receiving_binding_changed` for an
exact received delivery, its first response is `agent_message_recover` on that
delivery. Preserve the exact delivery and watch; do not re-register, duplicate
the receipt, review, or result, or infer transfer or authority. Continue with
the correlated result and disposition only after recovery succeeds.

On the ordinary HTTP/OAuth surface, `work_append` is only for exceptional
provenance, investigation, or legacy reconciliation on writable admitted work;
the required closed purpose makes that intent explicit. It must not store
current progress, findings, verdicts, final/current results, or ordinary agent
messages. Supply the observed work revision and one durable OperationId; the
edge resolves the current grant/version and applies the protected-effect,
provider-effect, and exact-readback rules. Reuse only that OperationId when
reconciling an ambiguous result; an idempotent replay returns its recorded
outcome.

The managed launch-bound legacy `work_append` is narrower: the launcher locks
writes to the active WorkId, and success requires the provider effect and exact
story/work readback. It takes no caller-observed revision and provides no durable
operation identity or idempotent replay contract. On either surface,
`unknown` means an effect may have happened: reconcile current evidence and
never blindly retry. `denied`, `stale`, and provider failure are not success.

Managed legacy active-inbox behavior is instruction-led and applies only to
bounded compatibility, investigation, or recovery. Ordinary agents use the
dedicated message tools. There is no daemon, generic inbox scan,
inactive-session wake, or authority inferred from message delivery. A sender's
successful send is not recipient pickup.

Typed `FINDINGS` at review, message, and handoff boundaries require an attributable
`ACCEPT`, `CHALLENGE`, `NARROW`, or `NEEDS_EVIDENCE` disposition. Accepted findings
must link an existing exact WorkId, record an authorized self-owned create, or preserve
an attributable proposed-owner route; cross-owner work remains a proposal. Durable
capture lives only in the full task-control capsule CAS. It never automatically creates
work, assigns an owner, grants authority, or introduces a second finding store.

## Legacy references

Provider source and attachment tools are retired from the current MCP surfaces.
Legacy task GIDs and URLs resolve locally to WorkIds; provider identity is not a
runtime authority.

## Surface boundaries

- The HTTP/OAuth edge is the ordinary workspace surface and uses authenticated
  principal admission plus explicit WorkIds.
- Managed task-bound Codex uses the launcher-controlled STDIO surface with
  injected active/reference WorkIds.
- Context-only launches expose only the minimal current-work/history view needed
  by that context contract.
- The development MCP owns local selected-test check, commit, explicitly non-authoritative
  diagnostic full-suite, and run-status mechanics; GitHub Quality owns full qualification;
  it grants no product work authority.

The executable factories, schemas, allowlists, and tests are authoritative for
the exact inventory on each surface. Documentation describes stable semantics;
it must not replace those owners with a volatile hand-maintained tool count.
