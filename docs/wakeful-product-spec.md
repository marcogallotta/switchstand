# Wakeful product specification

Status: product draft for prototype feedback and Human Input. This document specifies the intended
full product; it does not authorize implementation, scheduling, credentials, service mutation, or
agent activation. The existing prototype remains deliberately smaller and inert.

## Objective

Wakeful makes relevant external events start or resume the right agent workflow without Marco
having to poll, relay, or repeatedly say “resume.” The first product must wake on:

- an inbound human message;
- a review request or terminal review result;
- an agent or delegated worker completing, failing, or requiring input;
- an audit becoming due; and
- CI completing, failing, or requiring action.

Future sources must be addable through a producer contract rather than changes to Codex- or
Claude-specific orchestration. Wakeful is an event and delivery capability, not a new authority
root, work tracker, scheduler for arbitrary jobs, or general observability platform.

## Product boundary

Wakeful owns the path from a trustworthy occurrence to durable, observable delivery intent:

```text
source -> producer adapter -> neutral event journal/outbox -> routing policy
       -> Codex or Claude delivery adapter -> agent receipt/disposition
```

Source systems remain authoritative for the underlying fact. Switchstand remains authoritative for
WorkId, current assignment, grants, review/message lifecycle, and protected effects. Agent clients
remain responsible for starting or resuming their own sessions. Wakeful must not infer work
authority from an event, and a successful delivery must not be reported as accepted work or a
completed outcome.

The full product may reuse the prototype's local storage contract initially, but its durable event,
subscription, and delivery semantics must not depend on SQLite, systemd, Codex, Claude, Asana, or a
particular transport.

## Required journeys

### Human message

When Marco sends a message to a named agent or current work conversation, one durable event is
created after the message is durably accepted. The configured recipient is awakened or the event
remains visibly pending. The agent receives only a bounded reference and re-reads the canonical
message/current work before acting.

### Review lifecycle

A routed review request can wake an eligible reviewer. A terminal verdict wakes the exact source
work owner that owns the outcome watch. Registration, dispatch, receipt, pickup, verdict, and source
reconciliation remain distinct states. This specification does not replace or modify mailbox or
review routing.

### Agent lifecycle

Completion, failure, cancellation, and human-input-required transitions from a child or delegated
agent can wake the owning coordinator or source-work owner. An idle session alone is not completion;
the adapter must use the strongest terminal state the agent system actually exposes.

### Audit due

A durable due-time record emits an event once when its due condition becomes true. This is an
event-driven timer/scheduler boundary, not an agent repeatedly polling a task list. Changing or
cancelling the due record before emission must be reflected without leaving a phantom wake.

### CI lifecycle

A provider webhook or equivalent trustworthy completion signal creates an event tied to the exact
repository, workflow/check identity, and immutable candidate/base-head pair when relevant. The
agent re-reads canonical CI state before relying on it. Wakeful does not turn a green notification
into merge or deployment authority.

### Future event

A new producer supplies the same neutral envelope, a stable occurrence identity, a canonical
readback reference, sanitization rules, and an explicit routing configuration. Adding it must not
require a change to either agent adapter.

## Neutral event contract

The logical contract is versioned independently of transport. It retains the prototype's small
sanitized core and adds only fields required for routing, deduplication, and canonical readback:

| Field | Requirement |
| --- | --- |
| `schema_version` | Required integer; incompatible changes increment it. |
| `event_id` | Required stable occurrence identity; redelivery keeps the same identity. |
| `observed_at` | Required UTC timestamp for the observed occurrence. |
| `source` | Required stable producer/source identity, not an agent destination. |
| `subject` | Required stable subject identity, such as WorkId, run, review, or CI check. |
| `kind` | Required namespaced event type with versioned meaning. |
| `severity` | Required bounded classification; routing policy may use it. |
| `summary` | Required bounded, sanitized, single-line operator/agent hint. |
| `canonical_ref` | Required opaque reference for authorized current readback; never credentials. |
| `correlation` | Optional bounded identities such as WorkId, review ID, run ID, or candidate SHA. |
| `occurred_at` | Optional source occurrence time when distinct from observation time. |

Raw human text, raw provider/webhook payloads, journal lines, prompts, credentials, bearer tokens,
authorization material, and unrestricted principal data do not enter the neutral envelope. A
producer may retain source evidence in its canonical owner under that owner's access and retention
rules; Wakeful stores only enough reference data to retrieve it through existing authority checks.

The contract intentionally resembles the source/id/type/subject/time separation in CloudEvents,
but conformance to CloudEvents and an exact wire encoding are not requirements for the first full
slice. The existing v1 `WakeEvent` remains valid prototype evidence rather than being silently
declared the final schema.

## Delivery and lifecycle semantics

Wakeful must preserve these separately observable facts:

1. occurrence accepted durably;
2. event eligible under routing policy;
3. delivery attempted for a named adapter/target;
4. adapter accepted or rejected the delivery;
5. agent receipt or pickup, when the host can prove it;
6. disposition: acted, deferred, superseded, denied, stale, or no-current-authority; and
7. terminal workflow result, when one exists.

Delivery is replay-safe and duplicates are expected: `event_id` is the idempotency identity across
retries. A consumer must not repeat a protected effect solely because a delivery was repeated.
Wakeful may suppress repeated observations of the same condition, as the prototype does, while
preserving distinct state transitions and recovery. It must never mark delivery complete merely
because an adapter process was invoked.

If Wakeful cannot establish current routing, adapter availability, or a required canonical read, it
records a truthful pending/failed/unknown state with a bounded reason. It does not silently drop the
event or invent a new identity to escape ambiguity. Retention, retry limits, dead-letter behavior,
and escalation thresholds are part of the unresolved Human Input below.

## Agent-neutral adapter contract

Codex and Claude adapters implement the same boundary:

- input: neutral event plus configured target binding;
- preflight: confirm target/session mechanism is supported and credentials are available without
  exposing them to the event;
- deliver: start, resume, or steer through the host's supported mechanism;
- return: an attributable delivery result with adapter, attempt identity, time, and bounded reason;
- readback: when supported, establish receipt/pickup separately from transport acceptance; and
- recovery: reconcile an ambiguous attempt before a new attempt can cause duplicate work.

Adapter-specific session IDs, CLI flags, API keys, OAuth credentials, and host commands remain
inside the adapter. The neutral event does not contain a Codex prompt or Claude command. A common
bounded wake instruction tells either client to read `canonical_ref`, recover its durable work and
owned watches, verify current authority, and continue only authorized work.

Current platform evidence shows that the mechanisms need not be identical: OpenAI agent sessions
can receive follow-up input and expose lifecycle webhooks, while Claude Code can resume a named
session from its CLI. Those are candidate adapter seams, not a settled credential, hosting, or
delivery design.

## Authority and safety invariants

- Wake is attention, not assignment, permission, approval, review PASS, merge authority, deployment
  authority, or provider-write authority.
- The receiving agent must re-read the canonical work/message/review/CI record and current grant
  before consequential action.
- `no_current_authority`, `stale`, `denied`, missing capability, and ambiguous delivery are durable
  dispositions, not reasons to broaden access or retry with a new identity.
- One event cannot smuggle raw instructions around the current work and authority model.
- Producer authentication establishes the source of an occurrence, not permission for the eventual
  agent effect.
- A plain STOP pauses the affected new Wakeful action/delivery and causes the current owner to
  listen and re-ground. It does not terminate workers or erase pending events. CANCEL or STOP WORK
  explicitly targets work/delivery for best-effort termination. A global Wakeful disable is a
  separate explicit operator control that suspends all new delivery while preserving pending truth
  for deliberate recovery.
- Secrets are supplied by the host/adapter and never stored in event payloads or status views.

## Routing and configuration

Routing is configuration over event metadata and durable work ownership, not logic embedded in
producers. At minimum it can match `source`, `kind`, `subject`/correlation, severity, and named target
class. It must expose which rule made an event eligible and which adapter/target was selected.

The product must allow a new event kind and producer without editing Codex/Claude adapters, and a
new agent system without editing producers. Invalid or unmatched events remain inspectable. A route
change applies prospectively unless an explicit replay operation identifies the exact pending
events affected.

Exact fan-out and ownership behavior is deliberately unresolved: some events may notify multiple
observers, while work-taking events may require a single claimant. The implementation must not
choose broadcast or claim/ack globally before Human Input.

## Operator experience and observability

An operator must be able to answer, for an exact event: what happened; which canonical record owns
truth; why it matched; where delivery stands; whether an agent received or acted; the next retry or
escalation; and how to suspend, replay, or retire it safely. Aggregate views must show pending age,
failed/unknown delivery, adapter health, deduplication, and queue depth without exposing payloads or
secrets.

The first prototype-feedback slice may provide this through a bounded command/status document
rather than a UI. The final choice between CLI, MCP surface, local status page, or another operator
view remains Human Input. A green process/queue metric is not proof that an agent journey worked.

## Reliability and recovery

- Accepting an occurrence and recording its initial delivery intent is atomic, or the source has a
  replay/readback route that closes the gap.
- A crash after acceptance but before delivery leaves the event pending.
- A crash or timeout during delivery leaves the attempt ambiguous until adapter-specific readback
  resolves it; blind resend is forbidden when duplicate wake could duplicate work.
- Adapter restart preserves pending attempts and deduplication identity.
- Backpressure is bounded and visible; overload must not erase critical or human-input events.
- Recovery tests include stale owners/leases, duplicated source deliveries, out-of-order lifecycle
  events, unavailable agent hosts, and restart between every durable transition.

No automatic destructive recovery is implied. In particular, shared-ingress monitoring may wake an
operator or agent but must not automatically reset Tailscale Funnel/Serve configuration.

## Prototype evidence and limits

The landed inert prototype establishes:

- a versioned, sanitized, agent-neutral event envelope;
- SQLite persistence of cursor, condition state, bounded single-cycle lease, and delivery outbox;
- restart persistence, duplicate failure suppression, two-healthy-cycle recovery, bounded reads,
  lease expiry/takeover, and stale-owner rejection;
- an injected classifier for service transport, OAuth, provider, functional, missing-capability,
  recovery, and shared-ingress conditions;
- a one-shot host adapter with bounded systemd/journal/HTTP seams and a fixed read-only WorkId canary;
- external-ingress qualification through public DNS/IP pinning so same-host split DNS cannot falsely
  green Funnel; and
- a fixture/demo path that exposes sanitized pending events without scheduling or delivery.

On current `main` (`243e53963ca7616d3aab5793e033df9cbbeca6ed`), the focused prototype suite
`tests/test_wakeful.py`, `tests/test_edge_monitor.py`, and `tests/test_edge_monitor_host.py` passes
29 tests. This is landing evidence for local persistence/classification/host seams. It does **not**
prove source ingestion, actual Codex or Claude wake, delivery recovery, credentials, operator UX,
timers, or production activation.

## Prototype feedback round

The next product round should exercise one thin end-to-end vertical path without first building the
whole system:

1. Use a synthetic/manual event producer to write one event into a disposable outbox.
2. Deliver it through one disposable Codex adapter target using no production authority.
3. Demonstrate: idle target receives it; canonical readback is attempted; duplicate delivery does
   not duplicate the workflow; target unavailable remains pending; adapter restart recovers; and an
   explicit suspend prevents delivery without losing the event.
4. Repeat the identical neutral event and status contract with a disposable Claude target; only the
   adapter changes.
5. Run a 15-minute feedback session with Marco using the exact-event status view. Capture: whether
   the wake arrived soon enough, whether the reason/action was understandable, whether duplicate or
   noisy wakes occurred, whether suspend/replay was safe, and which missing state forced transcript
   archaeology.
6. Only after feedback, select the unresolved policies below and write the implementation
   specification/activation plan for real sources.

The first feedback round is successful when Marco can trigger, observe, understand, suspend, and
recover the same synthetic event on both agent systems without polling or inspecting raw logs. It
is not necessary for that round to ingest every source or provide a polished UI.

## Delivery stages

1. **Feedback adapter slice:** synthetic producer, existing outbox, one disposable Codex adapter,
   one disposable Claude adapter, bounded status, no production source or authority.
2. **Real-source pilot:** one reversible source selected by explicit post-feedback product choice,
   with canonical readback and observable delivery lifecycle. The stage ordering does not preselect
   that source. Do not bundle all six source classes.
3. **Source expansion:** add message, review, agent, audit, and CI producers individually with
   source-specific replay/security tests.
4. **Operational activation:** reviewed credentials, service/scheduler ownership, monitoring,
   capacity/retention, disable/recovery, and live acceptance for both agent systems.

Each stage must be independently useful, reviewable, disableable, and recoverable. Inert landing,
activation, and reliance are separate claims.

## Acceptance criteria for the full product

- All required journeys can produce neutral events with canonical references and stable duplicate
  identity.
- At least one Codex and one Claude target can be awakened through independent adapters without
  producer changes.
- No active-agent polling is required to learn that a configured occurrence happened.
- Restart, duplicate, unavailable-target, ambiguous-attempt, and delayed-delivery cases preserve
  truthful durable state and do not duplicate protected effects.
- Event delivery never grants authority; a wake with stale/missing authority ends in an explicit
  disposition and no consequential effect.
- An operator can inspect one event from occurrence through delivery, receipt when knowable,
  disposition, and terminal result when applicable.
- Suspend/disable, replay, and retention behavior are tested against exact identities.
- Credentials and raw sensitive payloads are absent from the neutral event store and operator view.
- Real-source and real-agent acceptance evidence is recorded separately from local prototype tests.

## Human Input before implementation hardening

These choices are intentionally open. The feedback slice can remain option-preserving until Marco
chooses them:

1. **Urgency classes:** which event kinds require immediate wake, and which may be coalesced into a
   short digest/window?
2. **Ownership:** which events broadcast to multiple agents, and which require one durable
   claim/ack owner? What happens when the claimant dies?
3. **Service levels:** target latency per class; retry/backoff horizon; and when and how failure
   escalates to Marco.
4. **Wake without authority:** should the agent only record `no_current_authority`, notify Marco,
   or be allowed to request authority through an existing bounded process?
5. **Operator surface:** is a CLI/status document enough initially, or is a live UI/MCP status
   surface required before real-source activation?
6. **Delivery and credentials:** which supported Codex and Claude resume/start mechanisms are
   acceptable, where their target/session bindings live, and how credentials are provisioned,
   rotated, and revoked?
7. **Retention:** how long must events, attempts, dispositions, and source classes remain available;
   which classes require different durations; and what evidence may be compacted while preserving
   exact identity and outcome truth?
8. **Dead-letter and retirement:** whether a permanently undeliverable event has a separate
   dead-letter state; who may retire pending events; after what attempts, age, or explicit decision;
   and what status/evidence must remain after retirement. Pending events are not silently aged out.
9. **First real-source pilot:** after the feedback round, which one reversible source should be the
   first production-facing pilot? The specification does not default to messages, reviews, agent
   completion, audits, or CI.

## Non-goals

- Replacing Switchstand work, authority, messaging, review, CI, or audit truth.
- Generic workflow orchestration, arbitrary cron, or a universal event bus.
- Polling every provider because it is easier than adding a trustworthy event/replay source.
- Prompt payloads as a substitute for canonical state and current authority.
- Exactly-once claims across external agent systems.
- Automatically remediating outages or deploying changes from a monitoring event.
- Requiring Codex and Claude to share session storage, launchers, credentials, or host mechanics.
- Expanding the prototype before the deliberately small feedback round answers the open choices.

## Research disposition

- **USED — CloudEvents 1.0 specification:** its separation of occurrence/event/message, stable
  `source` + `id` duplicate identity, `type`, `subject`, and time supports a transport- and
  consumer-neutral envelope. Wakeful borrows the concepts without requiring conformance.
  <https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md>
- **USED — GitHub webhook best practices:** source authentication, prompt acknowledgement with
  asynchronous processing, stable delivery identity, and redelivery support the durable-ingest and
  replay-safe source boundary. <https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks>
- **USED — OpenAI Agents API session and webhook documentation:** follow-up input can continue an
  existing session and lifecycle webhooks avoid holding an event stream open; terminal outcome must
  be distinguished from idle. This is evidence for a Codex adapter seam, not a commitment to this
  API or credential model. <https://developers.openai.com/api/docs/guides/agents-api/sessions>
  and <https://developers.openai.com/api/docs/guides/agents-api/sessions/webhooks>
- **USED — Claude Code CLI reference:** explicit resume/continue and noninteractive invocation are
  evidence for a separate Claude adapter behind the same neutral contract. This does not prove
  receipt/readback or settle host credentials. <https://code.claude.com/docs/en/cli-usage>
- **REJECTED — a shared Codex/Claude launcher:** current sources establish different supported
  lifecycle mechanisms, so sharing a launcher would couple the neutral contract to the clients.
  The shared seam ends at event/delivery outcome semantics.
