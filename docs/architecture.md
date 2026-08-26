# Architecture

## Components

`service.py` owns the loopback HTTP process, periodically asks the engine to reconcile, and
serves the static operator UI. `engine.py` owns all state transitions and persistence.
`app_server.py` is a dependency-free synchronous WebSocket/JSON-RPC client for the small
Codex app-server protocol surface used by `CodexAdapter`. The browser performs polling and
submits narrow message and attempt-control requests.

## State model

The JSON snapshot contains one Work, a fixed map of two roles, ordered messages, and append-only
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
- A claimed live checkpoint; it must be performed and recorded separately with real evidence
