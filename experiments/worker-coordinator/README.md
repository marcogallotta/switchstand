# Worker coordinator experiment

This directory implements the reviewed PostgreSQL g5 authority boundary consumed by
`switchstand_worker`. PostgreSQL owns immutable admission, leases and fences, checkpoints,
candidate receipts, cancellation, terminal state, and the independent publication ledger.
Workers can submit a bounded candidate but cannot publish it.

Apply `0001_schema.sql`, `0002_worker_routines.sql`, `0003_publication_routines.sql`, then
`0004_privileges.sql`. The fixed routines run in `SERIALIZABLE` transactions through
`PostgresStore`. Worker, coordinator, and publisher bearer authority stay separate.

The admitted repository policy is intentionally printable-ASCII for prefixes and candidate
paths. PostgreSQL therefore uses literal prefix matching and deterministic ASCII folding rather
than locale-sensitive `LIKE` or `lower`; Unicode paths are rejected without creating work.

The publisher reconstructs the stored manifest, creates deterministic Git blobs/tree/commit,
records those object IDs, and then uses one atomic target-ref plus immutable-marker compare-and-
swap. A lost provider response remains `reconciling` until marker readback proves `applied` or
sealed-not-applied.

Run the focused test:

```sh
node --test experiments/worker-coordinator/worker-coordinator.test.mjs
```

The test dependency is pinned PGlite 0.4.0, whose embedded engine reports PostgreSQL 17.5. It
provides deterministic migration, transaction, privilege, cross-workspace, HTTP, and Python
worker-client coverage without installing a host service. It is not deployment evidence for the
required PostgreSQL 17.11 service. Production must rerun the migrations there.

No database credential belongs in source, tests, Asana, evidence, or the worker environment.
The coordinator runtime receives only `EXECUTE` on fixed routines and no direct table access.
