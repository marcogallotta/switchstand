# Database-first Stage 1

Stage 1 makes PostgreSQL authoritative for task titles, completion, and ordinary work discovery in one
offline, irreversible cutover. The landed code is inert: schema upgrade alone creates empty
tables and does not change authority. Production migration and service activation require their
own current approval.

## Authority contract

Before the cutover marker exists, title/completion reads and writes and broad `work_search` retain their existing
provider behavior. The one-shot migration performs a complete final scan of the admitted Asana
projects while the MCP service is stopped, then uses one PostgreSQL transaction to:

1. reuse existing Asana `work_handles` or allocate missing WorkIds;
2. insert the exact scanned corpus into `work_index`;
3. insert the `POSTGRES_AUTHORITY` row and durable cutover marker.

No import rows or authority state survive an aborted transaction. There are no online claims,
leases, deltas, per-item flips, dual writes, agent acknowledgements, or mixed provider/DB search.
Synthetic mailbox handles and Asana handles absent from the final admitted scan are not members of
the corpus.

After `POSTGRES_AUTHORITY`:

- `work_search` reads only the indexed PostgreSQL corpus, including title, completed filtering,
  pagination, routing and context projections;
- exact work reads still use Asana only for fields Asana still owns, then overlay DB title and
  completion;
- every returned revision is an opaque composite of the authority generation, DB row version and
  exact provider revision;
- title and/or completion updates use the existing durable effect identity but write PostgreSQL only;
- a patch combining either DB-owned field with any provider-owned field is rejected before
  journaling or effect;
- provider-backed creation uses a non-authoritative bootstrap name and admits the requested title
  and read-back work facts before reporting success;
- later Asana title or completion changes are ignored.

Provider-owned routing/context facts are refreshed by exact reads and successful MCP
updates so search never performs broad provider discovery. Exact provider reads cannot admit an
unknown WorkId after cutover.

Composite currentness is isolated in `WorkIndex`, so a later authority stage can replace or ignore
provider revision input without changing callers. The single authority generation binds every
admitted-corpus revision and search cursor; Stage 1 adds no later-stage state machine.

## Offline procedure and recovery boundary

Use the existing service-wide maintenance transaction to install and publicly verify the
`503 Retry-After` gate, then stop the MCP edge. With all other database clients stopped:

```sh
switchstand-stage1-corpus capture corpus-a.json --source-candidate RUNTIME_SHA
switchstand-stage1-corpus capture corpus-b.json --source-candidate RUNTIME_SHA
switchstand-stage1-corpus compare corpus-a.json corpus-b.json
switchstand-work-index-migrate prepare --confirm-offline --manifest corpus-a.json \
  --manifest corpus-b.json --expected-corpus-digest REVIEWED_CORPUS_SHA256 \
  --expected-exception-digest REVIEWED_EXCEPTION_SHA256 --receipt stage1-prepare.json
switchstand-work-index-migrate activate --confirm-offline --manifest corpus-a.json \
  --manifest corpus-b.json --expected-corpus-digest REVIEWED_CORPUS_SHA256 \
  --expected-exception-digest REVIEWED_EXCEPTION_SHA256 \
  --expected-prepared-digest PREPARED_IMPORT_SHA256 --receipt stage1-attempt.json
```

The read-only corpus command captures the deterministic union of admitted search and exact-readable
bound Asana work twice. The migration command verifies database quiescence and both matching,
reviewed manifests, including both the full corpus and explicit missing/noncanonical exception
digests, then writes a distinct create-new preparation receipt before freezing missing WorkId
bindings. That receipt binds the exact before/final/inserted handles and prepared digest; activation
uses its own distinct receipt. Activation rereads the same
manifests and bindings and fails before the marker on mismatch. It commits corpus and marker
atomically, then reads back both generations and the digest; verify the edge before removing
maintenance.

After ambiguous prepare output, use `prepare-reconcile --receipt stage1-prepare.json`.
Before authority, `prepare-cleanup` requires the same two reviewed manifests/digests and preparation
receipt, proves their exact binding partition under the Stage 1 lock, and removes only the recorded
inserted handles. Recovery reports exact applied state, `NOT_COMMITTED`, or `UNKNOWN`.
`NOT_COMMITTED` permits the same receipt-bound retry. `UNKNOWN` retains maintenance and forbids old
runtime restart, schema abort, or a fresh attempt until exact readback resolves it.
Pass `--confirm-offline`, both `--manifest` arguments, both reviewed digest arguments, and
`--receipt stage1-prepare.json` to `prepare-cleanup`; it accepts no weaker evidence set.

Before the authority transaction commits, discard the failed attempt and restart the old service.
After it commits, never restore the old title/completion/search runtime: retain the gate and repair forward.
An error once COMMIT may have been sent is `UNKNOWN` until a fresh connection reconciles both markers, their generation, and the exact corpus digest. Unreadable or inconsistent evidence stays gated and forbids blind rerun, downgrade, or snapshot restore. Exact absence plus unchanged pre-activation corpus proves nonapplication; exact committed readback selects forward-only recovery. After exit status 2 or process loss, run `switchstand-work-index-migrate reconcile --confirm-offline --receipt stage1-attempt.json`; do not activate again.
The Alembic downgrade refuses whenever the durable marker exists, even if `work_index` rows were
later removed. Direct reverse authority states are rejected by schema constraints.
Runtime reads observe both the active authority row and durable marker in one snapshot and fail
closed if either is missing or their generations disagree; marker damage can never restore provider
title, completion, or search authority.

## Qualification

Landing qualification uses the owned disposable PostgreSQL harness. Causal cases cover complete
import, duplicate rejection and rollback, exclusion of synthetic/foreign handles, DB-only search
with provider search unavailable, filtering and pagination, copied clean and messy project shapes,
composite stale detection, atomic DB-only title/completion mutation, mixed-patch rejection before effect, cold-reader
lookup, irreversible state, and downgrade refusal. This is landing/inert evidence only; it does not
claim that a production scan or cutover ran.
