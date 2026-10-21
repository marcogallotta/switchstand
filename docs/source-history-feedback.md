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
`include_notes=true` on the ordinary `work_get` to retrieve full current notes;
long notes are marked as a preview by default. Read full notes before a
consequential action or a claim that depends on their complete contents. Use
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

### Feedback capture and triage

While doing assigned work, notice concrete failures and hidden friction that delay or
endanger delivery: bad code or incomplete specs discovered by the author, serious review
findings, failures escaping earlier tests, deployment/resource faults, repeated work,
blockers, and avoidable process or test time. A routine decision, frustration alone, or
legitimate difficult work is not itself an issue. A corrected harmless typo can stay local.

Once a credible concrete failure is observed, respond to Marco first, then capture it before
unrelated work. Use the existing Tracker Issues V1-A `file_*` surface, or reuse a known exact
issue with recurrence evidence and definitive receipt. Preserve an unfinished capture across
interruption. Do not file again for every exchange, and do not create a parallel register.
Respect STOP and explicit no-logging. Missing exact timing or a complete RCA does not excuse
silence: label approximate time as measured, estimated/range, or UNKNOWN. Distinguish human
effort, agent effort, and critical-path delay; avoid counting overlapping waits twice.
Use `file_process` for procedure or review-burden failures, `file_performance` for avoidable
time loss, and `file_bug` for software defects. Name the actual failure, not its setting:
a Human Review document that wastes human review time can be a process failure without being
a routine Human Review outcome.

Record briefly what failed, its source, consequence, time impact, workaround, and proposed
clearing action. Give separate LOW/MEDIUM/HIGH information quality and advisory priority with
reasons. Preserve human priority. Retain all trial reports; no automatic culling or ignoring.
Active human blockers or serious live damage need prompt attention through the existing
Coordinator route and an accepting correction owner. A sent message is not pickup or repair.
For issues outside your authority, route evidence to the current owner without editing it.

Triage closes only with evidenced resolution, proven same-cause duplicate, accepting owner and
next action, reasoned deferral with revisit condition, or exact blocker and clearing condition.
NEW/RELATED classification or promotion alone does not establish exit from TRIAGE or a fix.
Sample ordinary work that produced no report when evaluating this rule; review captured reports
alone cannot reveal missed failures. A synthetic classification test does not prove real filing,
owner pickup, or adoption. Record those as NOT_RUN until observed.

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

The managed launch-bound `work_append` is narrower: the launcher locks writes to
the active WorkId, and callers supply its observed revision and one durable
OperationId. Success requires the protected provider effect and exact story/work
readback; an identical retry replays the recorded outcome. On either surface,
`unknown` means an effect may have happened: reconcile current evidence using
only the same OperationId, never a new one or a blind retry. `denied`, `stale`,
and provider failure are not success.

For all MCP effects, inspect the full `CallToolResult`: `isError` and content
before `structuredContent`. A non-error result is definitive without another
client `work_get` only when its structured semantics prove the requested
postcondition. A protected effect requires `status=ok`, `effect=applied`, the
matching OperationId and target, and an authoritative receipt or resulting
revision. Registration requires `status=ok` and the returned actual name to
match the requested name. The server's own readback remains mandatory; this
rule removes only a duplicate client read. Missing fields, a mismatch,
`unknown`, interruption, eventual consistency, or a composed multi-effect
operation still requires bounded reconciliation/readback.

Independent effects on different targets may be issued together through one
`functions.exec` orchestration when they are already known safe to parallelize.
Give each effect its own OperationId and inspect each complete result. Preserve
serial order for same-target effects, dependent effects, or any operation whose
safety or ordering is not established.

Managed legacy active-inbox behavior is instruction-led and applies only to
bounded compatibility, investigation, or recovery. Ordinary agents use the
dedicated message tools. There is no daemon, generic inbox scan,
inactive-session wake, or authority inferred from message delivery. A sender's
successful send is not recipient pickup.

When Marco says `poll` or `keep polling` for an exact inbox, message, or review, keep that exact
watch active while the session can run: use supported bounded waits, reread the exact surface,
and continue until terminal result, explicit human stop/pause/reassignment, or a real
access/execution blocker. Empty reads and arbitrary check counts are nonterminal. Persist the
exact watch for re-entry if the session ends. A Scheduled task or condition watch is a different
future-running product and never substitutes for this live obligation unless Marco explicitly asks.

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
