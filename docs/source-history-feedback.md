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

### Feedback capture and triage

Capture concrete failures encountered doing assigned work, including author-observed code
smells/overengineering, incomplete or wrong specs, alarming review findings, serious corrected
near misses, deployment/resource failures, blocked workarounds, and avoidable process/test delay.
Try to notice hidden friction without waiting for a reprimand: repeated rereading, squeezing
updates to fit, reconstructing current commitments, or bookkeeping displacing execution. Record
the actual obstruction; task length or legitimate difficult implementation/review alone is not
a failure. A harmless corrected typo stays local. No compulsory audit or RCA before reporting.

Capture the underlying issue once. Reuse a known issue through authorized work/results and the
existing parent/owner handoff; add useful recurrence evidence instead of filing the same issue
for every exchange. Keep the known-ID check bounded. Use the applicable existing Tracker Issues
V1-A `file_*` surface for a distinct issue, inspecting the full result and definitive receipt.
Those filing tools create before suggesting duplicates: their suggestions do not prevent a new
record. Do not build a parallel register or turn every observation into a new task.

Briefly give expected versus observed behavior, affected reference, consequence, approximate time
lost, workaround, and smallest clearing action/owner pickup needed. Even two avoidable minutes
matter. Label time as measured, estimated/range, or UNKNOWN; missing timing or root cause does not
block useful evidence. Separate human effort, agent effort, and critical-path elapsed time. Count
recurrence/cumulative cost when supported; overlapping waits are not added as wall time. Distinguish
observation from hypothesis. Do not delay assigned work to reconstruct an exhaustive timeline.

During the bounded trial, rate every captured report on two independent LOW/MEDIUM/HIGH axes,
with brief reasons in the existing notes/result:

- **Information quality:** usefulness and support for understanding/correction. HIGH is concrete
  and supported; MEDIUM has useful information with material gaps; LOW lacks usable supporting
  information. Severity, polished prose, duplication, and missing precise timing do not determine
  quality. A recurrence can add valuable evidence.
- **Advisory priority:** urgency/importance from consequences, blocked work, recurrence and
  cumulative time. HIGH needs prompt attention; MEDIUM is actionable without urgent impact;
  LOW has limited current impact. Preserve human-set priority; no automatic numeric mapping.
  Weakly described credible human blockers still need immediate attention and evidence gathering.

Preserve original attributable ratings alongside later assessment. Retain all trial reports;
no automatic cull, deletion, or ignoring LOW/MEDIUM. Bound cohort and review effort before a
trial. Independently assess underlying evidence before seeing reporter ratings, across bands
and sources; compare later outcomes where available. Check bounded ordinary-work episodes that
produced NO report too, to find evidenced missed signals. Count misses, unsupported reports,
disagreement, missing evidence, and reporting/review overhead. Agent agreement is not ground
truth; consequential disagreement returns to Marco. A small synthetic test proves neither
natural adoption nor rare-failure safety. Filtering, deferral, and prospective non-reporting
remain separate later human decisions; captured reports alone cannot validate unseen failures.

Use affected authorized work/result and the existing parent handoff. The accepting parent obtains
correction-owner disposition. For a foreign known issue, send evidence to its owner rather than
editing it without authority. Missing pickup/route remains an explicit unavailable obligation.
An active human-work blocker, serious live damage, or credible severe near miss gets immediate
attention through the existing Codex Coordinator route, with impact and needed action; tell Marco
if his work is blocked or pickup fails. Continue unaffected authorized work. Existing incident
rules still apply; reporting grants no repair, deployment, or takeover authority.

Triage must produce an evidenced closure, proven same-cause duplicate, accepting owner with next
action, reasoned deferral with revisit condition, or exact blocker and clearing condition.
NEW/RELATED classification or promotion alone does not prove exit from TRIAGE, priority assignment,
owner pickup, or repair. Distinguish recorded, sent, accepted, corrected, and verified outcomes.

Preserve actual Human Review decisions on their affected work. A separate feedback issue needs
a concrete failure or reusable lesson; approval, frustration, apologies, and status alone are
not new issues. Respect STOP and explicit no-logging direction. No random exchange sampling or
mandatory per-reprimand/per-Human-Review issue. Higher-priority active session instructions remain
binding until corrected at their source; editing this document does not retroactively waive them.

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
