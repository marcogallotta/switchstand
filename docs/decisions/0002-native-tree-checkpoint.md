# ADR 0002: Gate the native agent-tree surface on live protocol evidence

- Status: Accepted
- Date: 2026-08-26

## Context

The fixed two-role prototype proved a local reliability slice, but it does not demonstrate
that an operator can see and address the real Codex agent tree. App Server now documents
native thread status, spawned parent lineage, descendant filters, pagination, and exact native
turn controls. Designing a new topology before those surfaces are observed would turn missing
evidence into product architecture.

## Decision

Add a separate Stage A protocol checkpoint without changing the current service or browser
path. Enumerate all documented source kinds explicitly, exhaust pagination, construct spawned
lineage only from `parentThreadId`, and preserve native status names. Generic `forkedFromId`
history is not parent-child evidence. A nonempty `sessionId` is required on every thread but
remains opaque per-thread evidence; equality across threads is not a lineage invariant. Runtime
`idle` is not semantic done.

For exact native controls, start normal input on an observed idle thread, steer an observed
active thread only with its exact in-progress turn id, and interrupt only an exact thread/turn.
Do not retry a request through a different mode when the native precondition changes.

Stage B may replace the synthetic main surface only after a real socket exposes one root and
spawned descendant with reliable lineage, complete pagination/source coverage, and native
status evidence. Fixture-backed protocol tests cannot satisfy that gate.

## Consequences

The repository can test the native protocol seam without prematurely coupling it to the old
attempt engine or adding another durable state model. The current browser still exposes the
fixed-role spike. A live environment with an App Server and a spawned tree is required before
browser implementation or browser checkpoint claims continue.
