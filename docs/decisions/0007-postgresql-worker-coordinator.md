# Decision 0007: PostgreSQL worker coordinator

## Context

The merged local worker has no durable coordination or GitHub authority. Its reviewed protocol
requires immutable task/repository admission, server-time leases and fences, crash-safe candidate
storage, and publication reconciliation that survives worker and coordinator process death.

## Decision

Add a separate experimental coordinator under `experiments/worker-coordinator`. PostgreSQL g5 is
the sole durable authority. Runtime access is limited to fixed `SECURITY DEFINER` routines;
workers submit inert candidates, while a coordinator-owned publisher performs GitHub effects
from the durable publication plan.

Publication creates deterministic Git objects, stores their exact IDs, and atomically updates
the expected candidate ref together with a unique immutable marker. The marker is the provider-
side close fence for lost responses, delayed requests, stale heads, and process death. Time alone
never proves that a GitHub request did not apply.

PGlite is pinned only for local/CI PostgreSQL-compatible tests. It does not replace the required
durable PostgreSQL 17.11 deployment or count as live persistence evidence.

## Exclusions

This does not alter the Switchstand UI, native mode, or local worker contract. It adds no worker
GitHub credential, D1 fallback, generic repository authority, automatic merge, root service, or
browser control.
