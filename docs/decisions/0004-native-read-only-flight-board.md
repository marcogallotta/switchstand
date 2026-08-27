# ADR 0004: Add an explicitly selected native read-only flight board

- Status: Accepted
- Date: 2026-08-27
- Authority: Asana task 1217910176021830 and GitHub issue #9

## Context

The fixed two-role surface proved useful failure-control and durability mechanisms, but it does
not show the real Codex agent tree or the work trail that an operator needs while agents run.
Stage A proved the native lineage/status protocol on exact PR4 head
`8670f50b629ae3f201d5eed3aa04fc92afa9888b`; PR4 was merged. That evidence does not claim a fresh
live run of later main head `6f845807b987aa77fc6b7527cc3d1bcd0165247d`.

## Decision

Add Stage B1 as a mode selected only with `--native-root-thread-id`. The default remains the
legacy two-role reliability spike. In native mode, poll the exact root with
`thread/read(includeTurns=false)` and fully paginate descendants with
`thread/list(useStateDbOnly=true)`. Show native `parentThreadId` lineage, exact native status and
flags, observer freshness, age since successful observation, and a bounded in-memory trail of
the latest 50 differences actually seen between polls. Native identifiers are represented only
by safe run-local references; transcripts are not exposed.

The mode is read-only. It does not resume or subscribe, write durable product state, or expose
message, steer, stop, redirect, or replace controls. It does not infer completion, progress,
failure, staleness, blockage, intent, or percentage complete. Because root and descendants come
from different endpoint calls, a poll is not atomic; the difference trail can miss or collapse
intermediate changes and is not a native event log. Consecutive observed-active time is not
measured execution time.

All operation is user-owned and loopback-only. No `sudo`, root-owned files, privileged ports,
system-wide install, or root-managed service is part of the design.

## Consequences

The native flight board supersedes the synthetic primary surface only when explicitly selected.
The old engine remains intact as a reliability spike. Controls, inferred semantic states,
durable native history, and general orchestration require later decisions and cannot enter B1.
