# Product boundary

## Operator problem

An operator needs to see whether real Codex agents are working and how they relate without
opening hidden threads or mistaking a silent interface for stalled work. The surface must show
only what App Server actually exposes and distinguish observed activity from semantic claims.

## Intended experience

When native mode is explicitly selected, Switchstand presents one exact root and every observed
spawned descendant as a read-only flight board. The operator can see:

- `parentThreadId` lineage, exact native thread status/flags, and a separately labelled
  content-free latest-turn status observation;
- whether the observer is connected, available, current or historical, or reporting an error;
- age since the last successful observation; and
- a chronological trail of differences Switchstand actually observed between polls.

The trail is not a complete native event log: polling can miss or collapse intermediate
changes, and root and descendant data come from different App Server endpoints. Silence and
native `idle` are not translated into done, stale, failed, or blocked. Consecutive
observed-active time is observation evidence, not measured execution time.

The operator may select one agent from current connected observation evidence. The browser
stores only the opaque observation/agent reference pair. Exact input starts a turn when the
current target has no in-progress turn, or steers the sole exact in-progress turn. Thread
status remains independent: `idle` plus `inProgress` is valid.
It does not retry, retarget, or fall back when evidence changes.

The default legacy mode retains the original fixed two-role experience and its message,
stop, redirect, and replace controls as a reliability spike. Issue #9 supersedes it as the
primary surface only while native mode is explicitly selected.

Legacy operations bound admission to their outer lock and App Server waits. Startup receives one
10-second monotonic cutoff through reconciliation; mutations, explicit reconciliation, reads,
and individual observer passes receive one five-second cutoff. A cutoff before durable acceptance
is unavailable. After durable acceptance, Switchstand records the exact acknowledged, rejected,
not-sent, or ambiguous prefix and returns that partial state. It never retries a failed setup or
mutation phase through a reconnect, fallback, or different target.

## Boundaries

Native mode observes one exact local root and its descendants over a Unix socket. It keeps only
bounded in-memory observation state and exposes exact current-target input plus one Stop action.
It exposes no redirect, replace, resume, subscription, transcript, or general lifecycle action.
Stop prepares and confirms cancellation of the exact in-progress turn mapped from current connected
board evidence. A request does not undo completed work or promise to stop background processes
or descendants.

Exact-turn resolution uses bounded `thread/turns/list` requests with `limit: 1`, descending
order, and `itemsView: notLoaded`. Item-bearing or ambiguous evidence fails closed. One fair
global probe per completed board pass supplies display-only status; Input and Stop independently
revalidate their one exact action target. Turn and thread ids are never returned to the browser.
The UI is unauthenticated loopback-only and must not be
exposed to a network. It does not authenticate against same-user local processes or browser
automation; stronger authentication is outside this checkpoint.

Switchstand does not decide what agents should build, infer semantic progress or intent, grant
remote-system authority, coordinate releases, schedule projects, manage users, or claim
durable distributed execution. It requires no elevated privileges.

The legacy cutoff is not a hard total-wall response deadline. Scheduler delay, JSON work,
synchronous snapshot and event persistence, directory barriers, and forced descriptor cleanup
are outside that claim. Required durable closure may therefore finish after the configured
cutoff. A persistence failure latches the running legacy process unavailable instead of exposing
possibly uncommitted memory or performing more App Server calls.
