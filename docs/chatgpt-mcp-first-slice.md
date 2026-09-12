# ChatGPT MCP first slice

Implementation task: `1218432551036197`. This candidate provides authenticated
request-context admission, current grants and one durable active-work append.
It adds a separate six-tool factory; existing managed Codex entry points and
credentials are not automatically changed or activated.

## Authority and effect behavior

`RequestPrincipal` reads only the MCP SDK's verified request context. The host
must configure the SDK's token verifier to verify the actual issuer, signature,
resource audience, expiration and scope. This adapter additionally requires the
configured issuer/resource/scope and stable subject/client identity. Model tool
arguments, message text, provider authors and project membership supply none of
these. A bare stdio connection has no authenticated request principal.

The host injects its resolver into `ChatGPTService`, and optionally supplies its
OAuth-configured `MCPServer` to `build_chatgpt_server`. Test resolvers use
`assurance="test"` and `test:` qualification references. They cannot qualify an
authenticated production grant. No test or static principal is a ChatGPT proof.

`GrantState.issue` is a trusted host API, absent from the MCP surface. Its
compare-and-replace transaction advances the principal's version exactly once.
Revocation, terminal state, expiration, changed active work and changed allowed
operations invalidate old authority. Revocation serializes with an already
admitted in-flight append; it takes effect before any subsequent append enters.
Source reads require authentication but remain useful without a current grant.
Work reads require a current grant and only cover its active work and references.

Append admission checks current principal, grant/version, active opaque WorkId,
operation, qualified surface, canonical source, terminal state and observed
revision. The provider has no atomic compare-and-append API: the revision is a
fresh preflight, not a claim that an unrelated external writer cannot race it.

The gateway commits an UNKNOWN intent before any possible provider send. Work
locks serialize competing effects; an unresolved intent blocks further sends to
that work even if principal, OperationId or payload changes. An identical logical
operation replay returns its earlier outcome and original OperationId. Text
equality does not identify a logical operation: a new OperationId may append the
same text after a resolved outcome if its current revision/grant preflight passes.
An unavailable journal preserves UNKNOWN prior-effect truth and requires
reconciliation; it never proves a replay was not sent. Success requires
exact story ID, target and text readback, persisted as a receipt. Restart does not
erase either receipts or UNKNOWN. Source task/story IDs never become WorkIds.

UNKNOWN requires trusted investigation of the exact provider target/story and
recorded intent. Do not delete intents, resubmit under a new ID or claim that
absence from one history page proves no effect. The model has no reconciliation
or journal-reset tool. `finish` is a trusted persistence operation, not permission
to fabricate a receipt. Downgrade refuses to discard nonempty grant/effect tables.

## Candidate verification

Use the repository's normal quality gate (`scripts/check` where supplied by the
current checkout, otherwise the existing CI quality service). The exact candidate
must pass Ruff, strict Pyright and the full pytest suite with PostgreSQL enabled.
The new database tests require `TEST_DATABASE_URL` pointing to
`switchstand_test`; mandatory database mode rejects a missing database instead of
silently treating skips as a pass. Tests exercise grant replacement/revocation,
wrong caller/work/op, stale revision, committed intent before send, concurrent
requests, cancellation, lost receipt storage, restart and no-new-ID bypass.
The stdio tests exercise the actual MCP client/server and closed tool schemas.

One fresh independent review is acquired by Coordinator under
`1218432628117496`. Give the reviewer the immutable head, task and current
branch/alignment amendments, actual code/config/tests and CI evidence. Work owns
corrections. Pure local tests may run in parallel with this review.

## Reviewed disposable provider qualification

Use an isolated checkout of the reviewed candidate. Preserve the live checkout,
live runs and production database. Supply the existing Asana token to the host
process and use a disposable `switchstand_test` database. Apply this candidate's
Alembic migrations to that database only. The reviewed harness is:

```sh
PYTHONPATH=src uv run python scripts/chatgpt-mcp-canary.py --task DISPOSABLE_TASK_ID
```

The task must already be an explicitly authorized disposable canonical task;
this command does not create/migrate Asana tasks. It provisions an expiring test
grant on the trusted host and serves stdio. On re-entry it preserves the grant,
OperationIds and prior effects. It fails on expired/different prior grants.

Connect a local MCP client. Read `grant_get`, `work_get`, both current notes and
paginated history, then exactly reread a material story. Submit one append with
a saved OperationId, current grant version, WorkId and revision. Verify the exact
receipt against the provider. Repeat/re-enter with that OperationId and verify
one effect. Negative calls using another work or stale grant must send nothing.
Record exact code SHA, tool inventory, task/story IDs and receipts. This proves
local/provider mechanics only. Never inject failure into a live production task.

Area-only canonicality/discovery depends on the separately owned approved
provider adapter `1218431674737116` and registry `1218432271807843`. Consume that
implementation and qualify fixture `1218431680474473`, wrong-project denial and
multi-home deduplication where needed. This slice does not add another registry,
model-facing search/assignment or migration. Existing old-project support remains
the baseline until the dependency lands.

## Actual ChatGPT connection qualification

Real use additionally needs the configured authentication server/token verifier,
resource endpoint and actual ChatGPT account. Secure MCP Tunnel can supply
transport; it does not supply end-user identity. Reuse the approved issuer and
account configuration when provided. Never ask the model to assert its identity
or treat tunnel/workspace access as the missing access-token validation.

After the reviewed server/auth metadata are stable, prepare the tunnel and plugin
connection on that exact host/account. Confirm the actual verified subject/client
from the server, issue its exact authorized grant through the trusted host path,
then test authenticated reads and one protected disposable append. Verify wrong
caller/work/grant and unqualified writes fail before sends. Inventory the actual
account's native Asana/GitHub tools, other MCP routes and credential-bearing
execution paths: unrestricted alternate writes invalidate a no-bypass claim.
Any settings change must use the existing exact approved qualification authority.

The current raw connector message bus remains the separately authorized fallback.
Own-active append is not arbitrary cross-agent inbox send; receipt, acceptance
and completed action remain distinct. Polling cadence and critical judgment
belong to the active agent, not this server. No inactive wake is implemented.

Record separate local and actual ChatGPT results. Do not land/activate this
candidate until its required independent review, CI, local Codex and real
ChatGPT capability evidence are complete. Reconcile then-current main and rerun
affected composed tests before non-force landing.

## Size and shared-path forecast

The base has 2,077 handwritten `src` Python lines. This Stage 2 MCP product slice
adds approximately 620 product lines plus an explicit disposable qualification
harness; it adds no Bootstrap launcher behavior. Existing Bootstrap code remains
unchanged and below its 2,400-line allowance. These modules are the consumed
grant/effect seam, not a full event ledger, role engine or lifecycle service.
Shared integration points are `state.metadata` / `work_handles`, additive migration
0002 and existing Provider source/append interfaces. Coordinate overlapping
schema/provider/config changes with the parallel Refoundation owner before
composed testing/landing.
