# Architecture

## Components

`service.py` owns the loopback HTTP process and serves the static operator UI. In default legacy
mode it periodically asks the engine to reconcile; `engine.py` owns that mode's state transitions
and persistence. In native mode, the service constructs exactly one `NativeBoard`, one
`NativeTargetTransport`, one `NativeInput`, one `NativeWorkbench`, and one
`NativeHttpDispatcher`. Board, input, and facade receive the same configured complete-pass
freshness limit.
`app_server.py` is a dependency-free synchronous WebSocket/JSON-RPC client for the small
Codex app-server protocol surface used by `CodexAdapter`. The browser performs polling and
submits narrow message and attempt-control requests.

The legacy `Runtime` creates one absolute monotonic deadline before its outer `RLock`
acquisition. Startup owns 10 seconds through startup reconciliation; every mutation, explicit
reconcile or snapshot, and observer pass owns a fresh five seconds. Nested creation, setup,
target, close, redirect, and multi-record reconciliation reuse that same shrinking deadline.
Lock and remote-wait admission stop at the cutoff. Already-admitted synchronous persistence and
forced local cleanup complete afterward when necessary, so the values are not end-to-end HTTP
latency promises.

`legacy_transport.py` keeps the deadline-aware legacy connection separate from native/P4. It
classifies request phases as `not_sent`, `acknowledged`, `rejected`, or `ambiguous`; the one-way
`initialized` notification is only `not_sent`, `sent`, or `ambiguous`. A target begins only after
the notification was fully sent. One connection may perform setup, its exact target, optional
naming, and graceful close while time remains. Cutoff skips the graceful close frame but always
forces reader and socket cleanup. No setup or mutation failure authorizes reconnect, retry,
fallback, or retargeting. Acknowledgement requires an exact matching integer JSON-RPC response
id, a mapping result, and exact string thread or turn identities; malformed or coercible values
remain ambiguous. Legacy response decoding rejects duplicate keys, nonstandard or non-finite
numbers, excessive nesting, parser-limit failures, and malformed notification envelopes; every
post-send decode failure is ambiguous and the owning session still forces descriptor cleanup.

`agent_tree.py` is a separate, fail-closed Stage A protocol layer. It does not adapt native
threads into the engine's roles or write a duplicate source of truth. It reads one exact root,
lists descendants with `ancestorThreadId`, supplies every documented `sourceKinds` value on
every page, follows `nextCursor` to exhaustion, and validates ancestry only through
`parentThreadId`. Every thread must carry a nonempty `sessionId`, but the value is opaque
per-thread evidence: neither it nor `forkedFromId` establishes spawned lineage.
Every root and descendant must also carry finite nonnegative numeric `createdAt` and
`updatedAt` protocol timestamps. Booleans, negative values, NaN, and infinities make the
complete pass unavailable without changing the last-good board or selection state.
One complete descendant pass is internally bounded to 100 requested records per page, 100
pages, and 10,000 descendant records. Required and optional protocol identities and pagination
cursors are limited to 1,024 characters. An over-limit response fails the complete pass rather
than truncating it. In the native production composition, connection setup, root read, and all
descendant pages share one three-second, 256-KiB response budget.

Native thread state remains exactly `active` (with documented flags), `idle`, `systemError`,
or `notLoaded`. Exact latest-turn state is represented separately; `idle` plus `inProgress` is
valid and neither field rewrites the other. For direct input, the checkpoint uses `turn/start`
only when exact metadata has no in-progress turn and `turn/steer` with the exact in-progress
turn id otherwise. Concurrent changes fail at App Server;
they are not retried through another mode. Stop uses the exact thread and turn ids.

`stage_a_evidence.py` owns retained-evidence projection and validation. `stage_a_probe.py`
owns bounded collection orchestration and the CLI over the same `CodexAppServer` and
`AgentTreeAdapter`. Together they record local observation windows, the exact safe subset of
native thread evidence, and cursor-presence/count/source-kind request facts for every exhausted
page. Exact native thread, parent, session, subscription, and notification identifiers remain
in memory for matching; retained evidence uses stable run-local pseudonyms consistently across
snapshots, revalidation, subscriptions, and related-thread events. Unrelated-thread status
events contribute only to a count.
It emits no previews, turns, prompt/output text, or socket paths. Native source evidence is a
strict projection of approved classification fields and bounded value shapes: unknown nested
metadata is dropped, while an approved path field is retained only as the constant
`[redacted]`. Snapshot or poll evidence and actually received
`thread/status/changed` notifications are separate fields.

`thread/read` and `thread/list` do not subscribe a new connection to thread events. Notification
mode therefore requires an explicit opt-in that calls `thread/resume` for only the exact observed
root and descendants, then fully re-observes the same tree before waiting. This changes runtime
loaded/subscribed state but does not add conversation history. Default mode never resumes.
Attempted and exact-id-acknowledged resume counts are fenced into every later failure result.
After any acknowledgement, failure evidence reports the runtime state change; when a request was
sent without an exact acknowledgement, it reports the effect as unknown and `mayHaveChanged`
instead of claiming no side effect. Failure disclosure uses counts, not raw attempted ids.
Native statuses in both paths are projected only to validated `type` and, for `active`,
`activeFlags`. Failures retain only stable probe-authored codes and messages, never raw protocol
values or exception text. Native-tree validation errors carry structured codes at their origin
and are emitted with an allowlisted phase such as `root_read`, `descendant_list`,
`lineage_validation`, or `timestamp_validation`. This makes a failed live gate diagnosable
without retaining ids, paths, statuses, flags, cursors, prompts, outputs, or exception strings.
Absence of a notification never changes a native status or implies stale work.

Stage B1 is enabled only by `--native-root-thread-id EXACT_NATIVE_ROOT_ID`. It polls
`thread/read(includeTurns=false)` for that root and fully paginated `thread/list` requests for
descendants, forcing `useStateDbOnly=true`. It does not call resume, subscribe, or any control
method. A fixed global budget probes one exact thread's content-free newest-turn metadata per
completed pass and rotates fairly; it never adds an O(agent-count) request loop. The existing
`/api/workbench` endpoint projects native lineage/thread status, separate turn status, observer
freshness, safe run-local agent references, and the latest 50 in-memory endpoint differences
instead of the legacy Work model. It exposes no raw native ids or transcripts.

`native_stop.py` adds the B2-only control path. Prepare resolves a run-local agent reference
only from current connected and present board evidence, performs one byte-capped
`thread/turns/list(limit=1, sortDirection=desc, itemsView=notLoaded)`, and binds the sole
in-progress exact turn into a short-lived opaque receipt. Commit atomically consumes the
receipt, revalidates the same exact turn with another bounded metadata read, and sends at most
one interrupt. A later bounded metadata read may move `requested` to
`confirmed`, `not_confirmed`, or `unknown`; it never retries or retargets. Receipts are capped,
expiring, process-local tombstones and contain no transcript content.

`native_selection.py` freezes the B3 `native-selection-v1` boundary. It purely re-resolves an
exact observation-run/agent-reference pair from a
supplied current B1 observation. Its closed output retains only the pair, connected/present
truth, and display values whose provenance is explicitly safe. Freshness depends only on the
supplied latest complete-pass completion time and configured maximum age. It owns no native
read, cache, persistence, HTTP, browser, transcript/input, topology, or Stop behavior.
Production composition uses that same resolver through `NativeBoard`; it does not add a second
target registry.

`NativeHttpDispatcher` alone validates native HTTP route, control, body, and closed result
contracts. The `BaseHTTPRequestHandler` adapter passes the raw request path including query,
all header tuples including duplicates, and one bounded raw body. It does not reparse native
JSON. Handled responses are written with the dispatcher's exact status, ordered headers, and
body. Native routes return immediately and never fall through to legacy behavior. Static and
legacy requests retain their existing paths.

`native_evidence.py` owns C1's bounded process-local evidence window. The workbench derives
identifier-free observation and status counts from its existing board snapshot and records only
closed action/outcome enums plus bounded duration. The browser can report only three fixed
same-origin signals: failed focus restoration, refresh coalescing, and user-cancelled Stop.
Events contain no prompt, output, draft text, native identifiers, paths, raw errors, screenshots,
or inferred intent. Consecutive identical transition/browser signals are coalesced, retention is
capped, restart clears the window, and recorder failure leaves product actions running while the
read-only summary reports evidence unavailable.

Each poll spans the tree endpoints plus at most one exact-turn metadata request and is not an
atomic global snapshot. The
difference trail records only changes visible in successive successful polls; intermediate
changes may be missed or collapsed. It is not a native event stream, and elapsed time is age
since observation; consecutive observed-active time is not time spent working. Transport
failure affects observer truth without rewriting the last native status. No poll result is
promoted to done, progress, stale, wedged, failure, or intent.

## State model

Native mode has no durable product state. It holds the latest successful observation and a
bounded trail in memory; restarting the service resets both. Native thread records remain the
App Server's truth.

The legacy JSON snapshot contains one Work, a fixed map of two roles, ordered messages, and append-only
attempt history. Each role stores a monotonically increasing `generation`, one selected
`current_attempt_id`, and a checkpoint with accepted message ids, the latest correction, and
latest accepted result. Each message has a role-local sequence and an explicit state such as
`queued`, `dispatching`, `delivered`, `completed`, or `unknown`. Each attempt records its exact
generation, Codex thread/turn ids, fence state, observation state, accepted output or stale
output, and errors.

Every mutation writes the complete snapshot atomically, then fsyncs one JSONL event. The
snapshot is authoritative; the event log is inspectable transition history, not a replay log.
This ordering can leave a snapshot transition without its diagnostic event after a crash, but
never makes the JSONL log more authoritative than the snapshot.

Any legacy save failure sets a process-lifetime persistence latch before a sanitized failure
escapes. Once latched, mutations, reconciliation, snapshots, and adapter-producing helpers make
zero App Server calls and return no possibly uncommitted in-memory snapshot. Startup failure is
fatal. A running observer wake becomes a zero-external no-op. Restart reads the last authoritative
snapshot: pre-call intents recover conservatively, while a snapshot that succeeded before an
event-append failure retains its exact committed closure.

On the currently verified Ubuntu/POSIX boundary, the legacy snapshot and event are current-user
regular files normalized to mode `0600` and opened without following their final path component.
Persistence orders snapshot-file fsync, atomic replace, parent-directory fsync, then event append
and fsync; failure of the directory barrier stops before the event is opened.

## Key invariants

- Each role's message sequence is increasing and dispatch is FIFO.
- At most one turn is dispatched on a role's selected attempt at a time.
- A completed turn updates the checkpoint only when both the exact attempt id and its captured
  generation still match the role and the attempt fence is open.
- Stop closes the fence and advances the role generation before requesting interruption.
- Replace targets only the role's exact selected non-live attempt and creates a new exact
  attempt. Redirect is the composed correction + stop + replace operation.
- Each submitted user text carries a durable marker derived from its message id. A lost start
  acknowledgement is never retried unless a complete `thread/read(includeTurns=true)` history
  using documented `userMessage.content` has no matching marker and the thread is idle.
- Restart converts interrupted `dispatching`, `starting`, and `stop_pending` mutations to
  `unknown`, then reconciliation uses app-server evidence without optimistic replay.
- Legacy initial creation may be deferred only when the current generation has no attempt, or
  its latest creation was exactly `thread_start_not_sent/setup_cutoff` and selection was cleared.
  Exact creation, rejection, or ambiguity consumes automatic creation authority.
- An exact thread id is saved before optional naming on the same client. If naming is admitted,
  that save carries a conservative `thread_name_pending` intent until the exact naming outcome
  is durably closed. Cutoff before naming records `thread_name_not_sent`; a closure-write failure
  can recover pending but never falsely recover not-sent after a possible naming call. Naming or
  close failure cannot erase or downgrade the exact thread acknowledgement.
- A waiting attempt without an exact turn stops locally with no interrupt. Redirect durably
  prepares the correction, one generation advance, the old fence, and the selected replacement
  before any external call, so the correction cannot reach the old target.

## Failure truth

Transport exceptions, missing acknowledgements, missing turns, and non-terminal states do not
become success. Ambiguous state remains `unknown`. A late completed result from a closed fence
or previous generation is retained as `stale_output` for diagnosis but cannot change the role
checkpoint. A successful interrupt request reports the local attempt as stopped; later turn
observation may still surface late completed output as stale.

The prototype does not coordinate concurrent service processes. Atomic replacement protects a
single writer from partial snapshot files, not from competing writers. Operators should run one
service per state path.

## Explicitly deferred

- Multi-process locking, transactional coupling of snapshot and event log, and log compaction
- State migrations beyond rejecting unsupported schemas
- Authentication, authorization, CSRF protection, TLS, and non-loopback deployment
- Configurable Works/role counts, role lifecycle, search, attachments, and rich streaming
- Push updates, durable external queues, retry policy, telemetry, and database storage
- Compatibility layers for alternative Codex transports or model providers
- Native input beyond exact current-target start/steer, Stop beyond exact B2 cancellation, and
  inferred semantic status
- Treating the poll-difference trail as complete history or durable audit evidence
- Replacing or removing the fixed-role reliability spike outside explicitly selected native mode
