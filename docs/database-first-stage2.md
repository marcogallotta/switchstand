# Database-first Stage 2

Stage 2 makes PostgreSQL authoritative for structured work metadata and dependency edges in one
planned single-host outage. The repository change is inert until the Stage 2 authority marker is
written. Production import and authority cutover require their own current Human Review.

This is deliberately a one-time migration, not a live migration framework. There is no dual write,
incremental catch-up, lease, claim, Asana projection, or reverse-authority path.

## Import worksheet

The operator stops the MCP service and every other database client, then generates a JSON worksheet:

```console
switchstand-work-metadata-migrate generate stage2.json --confirm-offline
```

Rows are keyed by stable WorkId. The generator copies only structured provider values: priority,
work type, review next action, legacy horizon/stage3 gate, exact provider revision, and exact
dependency identities. It never derives lifecycle, canonical root, responsibility, waits, or general
next action from notes, sections, placements, parentage, or assignee. Missing values are explicitly
`UNKNOWN`; terminal lifecycle is the one safe derivation from Stage 1 completion. Dependencies
outside the admitted Stage 1 corpus fail generation.

The operator may replace `UNKNOWN` values with known values. `WAITING` and `DEFERRED` require an
exact wait kind and reopen condition. `CURRENT` and `TERMINAL` require `NONE` wait values. A root is
`UNKNOWN`, `NONE`, or an admitted WorkId. `TERMINAL` is exactly equivalent to Stage 1 completion.

Validate without writing:

```console
switchstand-work-metadata-migrate validate stage2.json --confirm-offline
```

Validation requires the worksheet to cover the exact Stage 1 corpus once, re-reads every provider
revision and structured value, and requires exact dependency parity. Any changed revision, missing
row, duplicate, foreign dependency, or incoherent lifecycle/wait combination fails before effect.

## Atomic cutover

With the service still stopped, a separately authorized operator runs:

```console
switchstand-work-metadata-migrate activate stage2.json --confirm-offline --receipt stage2-attempt.json
```

The command repeats provider validation, locks and revalidates the complete Stage 1 corpus, updates
the existing versioned `work_index.routing` rows, inserts canonical `DEPENDS_ON` edges, and writes
the durable Stage 2 `POSTGRES_AUTHORITY` marker in one PostgreSQL transaction. `BLOCKS` is the
inverse query, never duplicate stored truth. Its receipt reads back both markers, generation, complete metadata projection digest, and dependency-edge digest.

Before the marker, ordinary behavior remains provider-backed. After it, migrated metadata and
dependency writes use PostgreSQL, mixed metadata-plus-provider-content patches reject before effect,
and provider metadata changes are ignored. Notes/comments and Stage 3 project/workset/placement,
parent, and content-authorization semantics remain provider-owned.

Pre-cutover failure leaves no Stage 2 authority and the worksheet can be regenerated. Once the
marker commits, downgrade refuses and recovery is forward-fix only. Activating the client-visible
metadata fields also requires the documented ChatGPT connection reinstall and fresh-chat schema
verification.
An error after COMMIT may have been sent is `UNKNOWN`, not a pre-flip failure. Keep maintenance until a fresh connection proves exact committed markers/digests or exact absence with unchanged pre-state; other results forbid rerun/rollback. Resolve with `switchstand-work-metadata-migrate reconcile stage2.json --confirm-offline --receipt stage2-attempt.json`.
