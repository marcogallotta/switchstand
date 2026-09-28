# MCP work, history, messaging, and source compatibility

Switchstand's current MCP model is provider-neutral and WorkId-based. Ordinary
clients discover or resolve admitted work, then use work, structure, history,
attachment, event, and protected-effect operations. Managed Codex receives its
active and reference WorkIds from the trusted launcher; those bindings, not tool
arguments or readable provider records, define its work boundary.

## Current work and history

Call `work_get(api_version="1")` for the launch-bound assignment, or pass an
admitted WorkId where the ordinary tool schema permits it. `item.id` is the
opaque WorkId. Use the returned revision when reading bounded history,
attachments, or exact events. A stale result means reread current work and
restart the affected paginated read; partial, malformed, unavailable, or stale
pages never prove complete history.

Ordinary workspace clients can use provider-neutral search and exact legacy
reference resolution where their current admission permits it. Resolution maps
an already identified provider reference to admitted work; it does not make a
provider task ID a WorkId or grant authority. Structure and search results are
discovery evidence, not reassignment, approval, or permission to write.

Before relying on material mutable evidence, reread its exact current work or
event representation. Readability does not imply authority, and status labels
do not establish that an external effect occurred.

## Durable messaging and feedback

Durable message tools are the current agent-to-agent messaging surface. The
work-addressed family sends to an admitted recipient work route and provides
pending, receive/recover, correlated result, and disposition operations. The
registered-agent family provides the equivalent lifecycle for an immutable
registered agent name when that route is appropriate. Delivery, receipt,
disposition, effect, and completion are distinct states.

On the ordinary HTTP/OAuth surface, use `work_append` only for bounded feedback
on writable admitted work. Supply the observed work revision and one durable
OperationId; the edge resolves the current grant/version and applies the
protected-effect, provider-effect, and exact-readback rules. Reuse only that
OperationId when reconciling an ambiguous result; an idempotent replay returns
its recorded outcome.

The managed launch-bound legacy `work_append` is narrower: the launcher locks
writes to the active WorkId, and success requires the provider effect and exact
story/work readback. It takes no caller-observed revision and provides no durable
operation identity or idempotent replay contract. On either surface,
`unknown` means an effect may have happened: reconcile current evidence and
never blindly retry. `denied`, `stale`, and provider failure are not success.

Managed active-inbox behavior is instruction-led. Agents check exact bound current
work/message surfaces during active work and on re-entry; history is opened only for
a bounded investigation/audit/recovery question that current durable state cannot
answer. There is no daemon, generic inbox scan, inactive-session wake, or authority
inferred from message delivery. A sender's successful send is not recipient pickup.

## Raw source compatibility

`source_task`, `source_stories`, and `source_story` are bounded compatibility
reads on the managed surface for exact provider records still needed by legacy,
reference, recovery, or failback flows. They are not the ordinary current MCP
mental model and should not be copied into new ordinary workflows.

Where supported, source reads accept an exact provider task identity already
supplied by the assignment or its evidence and enforce the configured provider
boundary. Page stories using only the returned cursor and observed revision;
restart after `stale`. Before a consequential claim based on mutable comment
content, reread the exact story. Source readability neither binds active work nor
grants an effect, and a provider task ID never substitutes for a WorkId.

Compatibility retirement is proof-gated: remove raw source reads only after all
required ordinary, recovery, and failback consumers have verified neutral
replacement coverage. Until then, keep compatibility use narrow and keep new
current guidance on WorkId-based APIs.

## Surface boundaries

- The HTTP/OAuth edge is the ordinary workspace surface and uses authenticated
  principal admission plus explicit WorkIds.
- Managed task-bound Codex uses the launcher-controlled STDIO surface with
  injected active/reference WorkIds; raw source reads exist there only for the
  bounded compatibility cases above.
- Context-only launches expose only the minimal current-work view needed by that
  context contract; history remains a separate bounded recovery/investigation capability.
- The development MCP owns local check, commit, quality, and run-status mechanics;
  it grants no product work authority.

The executable factories, schemas, allowlists, and tests are authoritative for
the exact inventory on each surface. Documentation describes stable semantics;
it must not replace those owners with a volatile hand-maintained tool count.
