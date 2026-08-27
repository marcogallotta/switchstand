# Product boundary

## Operator problem

An operator needs to see whether real Codex agents are working and how they relate without
opening hidden threads or mistaking a silent interface for stalled work. The surface must show
only what App Server actually exposes and distinguish observed activity from semantic claims.

## Intended experience

When native mode is explicitly selected, Switchstand presents one exact root and every observed
spawned descendant as a read-only flight board. The operator can see:

- `parentThreadId` lineage and exact native status/flags;
- whether the observer is connected, available, current or historical, or reporting an error;
- age since the last successful observation; and
- a chronological trail of differences Switchstand actually observed between polls.

The trail is not a complete native event log: polling can miss or collapse intermediate
changes, and root and descendant data come from different App Server endpoints. Silence and
native `idle` are not translated into done, stale, failed, or blocked. Consecutive
observed-active time is observation evidence, not measured execution time.

The default legacy mode retains the original fixed two-role experience and its message,
stop, redirect, and replace controls as a reliability spike. Issue #9 supersedes it as the
primary surface only while native mode is explicitly selected.

## Boundaries

Native Stage B1 observes one exact local root and its descendants over a Unix socket. It polls
read-only, keeps only bounded in-memory observation state, and exposes no message, steer, stop,
redirect, replace, resume, or subscription action. The service is an unauthenticated loopback
UI and must not be exposed to a network.

Switchstand does not decide what agents should build, infer semantic progress or intent, grant
remote-system authority, coordinate releases, schedule projects, manage users, or claim
durable distributed execution. It requires no elevated privileges.
