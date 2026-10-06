# ChatGPT MCP edge

This is Switchstand's authenticated HTTP/OAuth edge for ordinary ChatGPT and
Codex clients. It is distinct from the launcher-bound managed Codex STDIO
surface and from the local development MCP. Landing edge code or documentation
does not start a listener, configure OAuth, issue a workspace grant, or connect
a client. For a suspected live outage, use the
[live-incident runbook](operations-live-incident.md); metadata or transport
availability alone is not an authenticated health check.

## Current surface

The edge exposes mostly provider-neutral capabilities in semantic groups:

- repository bootstrap for an ordinary ChatGPT session that has no checkout;
- work discovery, exact legacy-reference resolution, reads, history, and events;
- grant-checked work creation, update, relation, and append operations;
- registered-name `agent_message_*` messaging (managed task-bound runtimes separately retain
  WorkId-addressed `message_*` tools), plus an exact destination-side request for host-approved
  cross-principal dead-name transfer; and
- required-result persistence.

Canonical work reads expose nullable server admission time. Existing and source-imported rows may
truthfully remain `null`; landing this schema change does not activate or prove the live client path.

The priority-claim and bounded priority-context tools remain default-off unless the resource edge
starts with `SWITCHSTAND_PRIORITY_CLAIMS=1`. Enabling that flag constructs both projections over
the canonical PostgreSQL repositories. It permits only the already-bounded launch-grant
`AGENT_RECOMMENDATION` write path; it does not create a trusted `HUMAN_PRIORITY` write seam.
Direct project-claim reads still require workspace read authority. A context request authorized for
an exact WorkId may include the claims attached to that WorkId's project memberships as explicitly
labeled context; it does not grant a project read or copy those claims onto the WorkId.
Because enabling changes the client-visible tool schema, activation requires an edge replacement,
a ChatGPT app/connection reinstall, a fresh chat, and exact schema and behavior verification. The
maintenance replacement applies the repository's current migration head before starting the enabled
edge. Clearing the flag in a later authorized replacement removes all three tools without deleting
stored claims; database downgrade is not the feature rollback path.

The executable inventory and client policy are owned by
`build_ordinary_tools` in `src/switchstand/chatgpt_mcp.py`, its edge tests, and
the repository's `.codex/config.toml`. Do not copy the full tool list into prose:
it changes as capabilities are added or retired. `repository_bundle_get` is the
bootstrap route for an ordinary ChatGPT session without a normal checkout; a
Codex session that already has the repository uses Git normally.

The edge does not create a second authority system. It resolves the verified
OAuth principal to the current workspace admission. Work tools apply the
existing WorkId, grant-version, revision, operation-identity, UNKNOWN, and
effect-readback rules. Trusted grant issuance is not an MCP tool. Provider credentials and grant
state stay behind the service boundary.

The DB-native ordinary edge exposes no provider-effect recovery tool. Frozen-cutover procedure uses
the exact pre-cutover provider-backed deployment for any provider readback. Archival or deletion is
a separate explicitly authorized operator action completed before the DB-native application is
installed. A surviving mismatch remains `UNKNOWN` and requires trusted operator adjudication without
a resend.

Raw `source_*` tools are not part of the ordinary current surface. They remain
available only on bounded managed/recovery compatibility routes described in
[MCP work, history, and compatibility](source-history-feedback.md). New ordinary
flows use provider-neutral WorkIds and work/history/event tools.

This private single-user deployment advertises `readOnlyHint=true` on every
current ordinary tool to work around ChatGPT prompting for routine calls even
when the app is configured to allow all tools. That client-facing permission
hint does not mean work or messaging operations are effect-free. Safety remains
deterministic and server-side: authenticated admission, exact authority and
targets, revisions, stable identities, transition guards, payload validation,
and UNKNOWN/readback rules reject invalid effects without relying on a prompt.

## Private configuration

The authorized host keeps OAuth client credentials, allowed immutable identity,
public resource URL, bind settings, database URL, and the purpose-specific read-only GitHub token
used by repository bundle resolution
outside the repository and logs. The public resource URL uses HTTPS and ends in
`/mcp`; the process binds only to loopback behind its reverse proxy. OAuth
metadata, authorization, callback, consent, registration, token, and MCP routes
must reach that same process. OAuth establishes client identity; Switchstand's
current workspace admission remains the authority for each read or effect.
The repository-bundle token is a fine-grained token restricted to
`marcogallotta/switchstand` with read-only Contents access; it is not the OAuth client credential.

FastMCP owns encrypted OAuth client registrations and token mappings in its
user-data directory. This is transport session state, not Switchstand authority
or a second grant store. Losing it invalidates sessions and requires clients to
reconnect.

The single-user edge stores one canonical, identity-and-scope-validated GitHub
credential behind those separate downstream mappings. A successful new browser
authorization replaces that credential, while existing client access and refresh
JTIs converge to it on use. Missing or expired JTI mappings are recovered only
from a still-valid signed downstream token and the validated canonical credential;
refresh metadata must also remain current, so rotation or revocation is not undone.
Client IDs, signed tokens, grants, and runtime identities are never merged. Refresh
rotation is serialized within the one edge process. A duplicate use of a just-consumed
downstream refresh token can replay its exact response for five minutes only while the
issued successor remains unconsumed and the client and scope request still match. This
128-entry cache is process-local and intentionally lost on restart; no replay tokens are
written to the OAuth store. Running overlapping edge processes against this file-backed
OAuth store is not a supported activation shape: its refresh locks and replay cache are
process-local.

The read-only edge doctor needs no database or provider credentials. Keep its
three protected OAuth values and the non-secret probe configuration in one
mode-`0600` environment file:

```dotenv
SWITCHSTAND_MCP_RESOURCE_URL=https://public.example/switchstand/mcp
SWITCHSTAND_MCP_BIND_HOST=127.0.0.1
SWITCHSTAND_MCP_BIND_PORT=8790
SWITCHSTAND_MCP_PUBLIC_URL=https://public.example/switchstand/mcp
```

Stateful product-currentness diagnostics are default-off. Setting
`SWITCHSTAND_PRODUCT_CURRENTNESS=1` requires the selected and running runtime SHAs,
run ID, and expected tools-schema SHA-256 through the
corresponding `SWITCHSTAND_PRODUCT_CURRENTNESS_*` variables. Startup rejects missing
or malformed values. This diagnostic construction binds those host values to the
authenticated principal, exact live FastMCP tool schema, outcome-action switch, and PostgreSQL
prerequisites. The optional
`SWITCHSTAND_PRODUCT_CURRENTNESS_QUALIFICATION_RECEIPT` and
`SWITCHSTAND_PRODUCT_CURRENTNESS_QUALIFICATION_KEY` values must be configured together as
absolute paths. When they are absent, `functional_proof` remains a blocker and this layer
cannot return `TRUE`. Selecting, creating, and retaining those private files is a separate
activation decision; this edge does not create either file.

`switchstand-stateful-qualifier schema-digest` reads the exact authenticated `tools/list` surface
used by this contract. Its `qualify` command requires one explicit disposable WorkId, a separately
denied WorkId, an attempt UUID and mode-0700 attempt directory, exact runtime/run and
schema expectations, and an existing mode-0600 seal key. It journals the immutable operation IDs
before the protected update; proves authenticated MCP, DENIED admission, STALE CAS, exact replay,
and current readback; emits the receipt create-new; then requires the same live adapter to evaluate
TRUE. An existing mismatched journal or any ambiguous/nonconforming outcome emits no PASS.
While product currentness is configured, the edge publishes the same narrow runtime-SHA/run-ID
readback used by the qualifier; an independently configured certification identity must match it.

Then the canonical activation check is:

```console
scripts/switchstand-edge-doctor --env-file /path/to/edge-doctor.env \
  --expected-sha "$EXPECTED_SHA" --repo /path/to/switchstand
```

After startup, `switchstand-edge-semantic-probe` is the separate authenticated semantic check.
Its only path is read-only and writes a create-new, mode-0600, directory-synced receipt recording the
runtime candidate/run, endpoint, exact `tools/list`, representative WorkIds/revisions, denials, and
transcript. The nonmutating path records only an expected principal; exact proof is `NOT_RUN`.
It accepts no mutation, replay, or restart options. A separate reviewed C2d2b package must keep
mutation loopback-only, require an exact `test:disposable` qualification and explicit OperationId,
and hard-disable production mutation. Before its first possible effect it must exclusively create
a mode-0600 receipt, fsync both file and parent, and terminalize it as `PASS`, `FAIL`, or `UNKNOWN`.
It may replay only after fully validating a first `status=ok`, `effect=applied` receipt whose
OperationId, principal, qualification, target, and effect identity match the request; ambiguous,
malformed, or non-ok outcomes are never replayed. Restart proof must bind the same candidate SHA,
runtime SHA, endpoint, prior receipt and readback, plus a different nonempty current run ID.
Neither receipt nor transcript may persist tokens or other secrets. Until that package is
independently reviewed, production mutation and exact authenticated principal proof are `NOT_RUN`.

Command-line resource, local, and public URL options remain available for
one-off checks and override values in the file. An explicit local probe URL
must be credential-free loopback HTTP at exact path `/mcp`.

## Maintenance-window replacement

`scripts/switchstand-edge-maintenance` is the sole repository-owned production
replacement transaction. It is inert until an operator invokes it with an exact
clean candidate checkout, staged launcher and their expected digests, the current
runtime and launcher identities, the exact FastMCP OAuth state directory, a new mode-`0700`
attempt directory, and the edge environment file. Its host defaults are the
existing `switchstand-chatgpt-mcp.service`, loopback Caddy admin API and public
Switchstand origin; changing that topology is separate work.

The transaction takes one fixed service-wide exclusive lock, independent of the attempt
directory, and writes a mode-`0600` atomic receipt. It accepts only the production edge's
canonical FastMCP state path and a candidate launcher that is byte-for-byte the current
launcher with its single exact runtime path retargeted to the candidate. Before replacement
and after each start, it binds the systemd `MainPID` command line to that launcher and derives
the process's effective FastMCP path from its initial environment; a mismatch cannot pass.
It proves the current four Caddy proxy handlers, inserts and publicly verifies a
first-priority `503 Retry-After` route covering every Switchstand MCP, OAuth and
metadata path, and only then stops the edge. While that gate remains publicly proven
and the systemd service is confirmed stopped, it runs the existing
`switchstand-upgrade-state --target production` rehearsal, backup, and shared-state
upgrade before any launcher swap or start. The receipt records `UPGRADE_PENDING`
before that forward-only command and `UPGRADED` only after it completes, so an
interrupted or failed migration remains gated and `UNKNOWN`, never a blind retry or
old-runtime rollback. It then snapshots the exact FastMCP state directory without
parsing or logging its secret contents, atomically swaps the launcher, starts the edge, runs the edge doctor locally, removes the
gate, and runs the public doctor. A definite failure before `UPGRADE_PENDING`
retains the existing safe recovery: when stop is definitely complete, it restarts
and verifies the compatible old runtime before ungating. Every failure or
ambiguity at or after `UPGRADE_PENDING` returns `UNKNOWN`, retains or reinstalls
the gate, and performs no automatic old-runtime restart. It never restores the
OAuth snapshot automatically because doing
so could discard registrations or token rotations accepted after the snapshot.
Snapshot restoration is a separate offline corruption-recovery action with explicit
session-loss consequences. Ambiguous mutation/readback or ambiguous rollback returns `UNKNOWN`
and retains or reinstalls the maintenance route. An interrupt after gate insertion follows the
same fail-closed path. Never blindly rerun an UNKNOWN;
inspect its receipt and live gate/service/launcher state first.

One narrow recovery mode exists only for an exact `UNKNOWN / UPGRADE_PENDING`
receipt whose shared-state effect can be disproved. With the ordinary edge lock and
the state-upgrade lock both held, it requires the exact old launcher and healthy
local/public service, database revision `0013_failure_journal`, absence of
`agent_mailbox_transfer_requests`, and an unchanged receipt preimage. It then writes
the terminal `FAIL / NO_EFFECT` receipt. Any mismatch remains `UNKNOWN`; this mode
does not retry the upgrade or change the service, launcher, database, or Caddy.

The retained snapshot, failed candidate state (when applicable), launcher backup,
and receipt stay in the attempt directory as recovery evidence. The tool does not
prepare candidates, launchers, authorization, client reinstall, or activation
approval. Landing it does not change the running service or Caddy configuration.
This single-runtime maintenance path remains the **current deployed production model**.
Agent-visible behavior during the gate is owned by
[live-incident operations](operations-live-incident.md#known-edge-maintenance-window).

The retired staged database-authority cutover is not part of the ordinary edge
replacement CLI. The zero-Asana migration owns its separate stop, backup, import,
validation, and rollback procedure. No agent announcement or acknowledgement
protocol is part of that single-host maintenance window.

The earlier deferral of zero-downtime authentication remains part of the decision history, but a
later explicit decision reopened Option C for **bounded inert single-host implementation** after the
corrected feasibility spike passed. The selected direction is a stable FastMCP authorization
service which retains the current signing key, encrypted OAuth/JTI state, canonical GitHub
credential, and per-request GitHub validation. Replaceable MCP edges delegate bearer verification
over an authenticated private loopback or Unix-socket protocol; they do not receive the signing key.
See the [decision record](deferred-zero-downtime-auth.md) and
[feasibility evidence](auth-split-feasibility.md).

This reopening does not change the running service, authorize activation, or make zero downtime a
prerequisite for ordinary maintenance deployments. Multi-host operation, clustering, active-active
authorization, distributed locking, and shared authentication storage remain explicit non-goals.

### Inert stable-auth layers

The repository contains a default-off production seam for the selected single-host split.
`stable_auth.py` builds the stable FastMCP authorization application and its private authenticated
introspection route. The same module supplies the strict edge verifier; every bearer check calls
the stable service under a verifier-owned two-second deadline (configurable only up to five seconds)
and retains no more than 4 KiB of streamed response data. The first excess byte closes the response.
Any unavailable, unauthorized, oversized, malformed, expired, wrong-scope, wrong-resource,
wrong-issuer, or wrong-subject response fails closed. The delegated edge builder
receives only that verifier and the resulting resource contract; it has no GitHub client secret,
FastMCP signing key, or authorization-state store.

Layer 2A adds three explicit, default-off commands:

```console
switchstand-stable-auth
switchstand-delegated-edge
switchstand-stable-auth-host --help
```

The first process owns GitHub OAuth and FastMCP state and binds only to
`127.0.0.1:8791`. The second owns the ordinary MCP/provider surface, binds only to
`127.0.0.1:8790`, and calls the first process for every bearer check. The current
`switchstand-chatgpt-edge` command remains the combined default and its environment contract is
unchanged.

The split intentionally uses authenticated loopback HTTP rather than a Unix socket. Both current
services and Caddy already use fixed loopback listeners; adding socket creation, stale-socket
recovery, and Caddy/service filesystem permissions would create another lifecycle boundary without
improving the single-user, single-host trust boundary. The stable process accepts private
introspection only with a shared high-entropy credential. Caddy's generated route set has no match
for that path.

Use distinct mode-0600 environment files. The stable-auth file contains only its required values:

```dotenv
SWITCHSTAND_MCP_GITHUB_CLIENT_ID=...
SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET=...
SWITCHSTAND_MCP_GITHUB_USER_ID=192548
SWITCHSTAND_MCP_RESOURCE_URL=https://public.example/switchstand/mcp
FASTMCP_HOME=/absolute/stable/auth/state
```

The delegated-edge file contains the resource URL, numeric GitHub user ID, and database
configuration and its purpose-specific repository-bundle GitHub read token, but **not** the GitHub
OAuth client secret, signing material, or `FASTMCP_HOME`:

```dotenv
SWITCHSTAND_MCP_GITHUB_USER_ID=192548
SWITCHSTAND_MCP_RESOURCE_URL=https://public.example/switchstand/mcp
DATABASE_URL=...
SWITCHSTAND_REPOSITORY_BUNDLE_GITHUB_TOKEN=...
```

Delegated-edge startup fails closed if GitHub client ID/secret or `FASTMCP_HOME` is present, catching
an accidentally shared environment rather than merely relying on operator convention.

Provision the shared internal credential into an existing owned directory. Commands output only
the path and SHA-256 readback, never the credential:

```console
switchstand-stable-auth-host credential-init /absolute/private/internal.secret
switchstand-stable-auth-host credential-readback /absolute/private/internal.secret
switchstand-stable-auth-host credential-rotate /absolute/private/internal.secret \
  --expected-sha256 EXPECTED_CURRENT_DIGEST
```

The file must be owned by the service user, regular, symlink-free, exactly mode `0600`, and contain
the generated 64-character URL-safe token format (including its minimum diversity check). Init and
rotation serialize through a persistent owned mode-`0600` no-follow sibling lock. Rotation compares
the expected digest and replaces the credential inside that same transaction; its receipt is bound
to the bytes written, so two concurrent callers cannot both win with the same prior digest.
Rotation is deliberately offline: stop both split processes, rotate with the last readback digest,
read back the new digest, then start stable auth before the delegated edge. The processes read the
credential once at startup; rotating under running processes would temporarily split their trust
state.

Generate candidates into a new empty directory; this does not install or apply them:

```console
switchstand-stable-auth-host render \
  --runtime-python /absolute/runtime/bin/python \
  --runtime-root /absolute/exact/candidate-checkout \
  --auth-environment-file /absolute/private/stable-auth.env \
  --edge-environment-file /absolute/private/delegated-edge.env \
  --internal-secret-file /absolute/private/internal.secret \
  --output /absolute/new/empty-output-directory
```

The result contains two user-systemd units and a Caddy JSON route fragment plus a digest receipt.
The fragment preserves the current public URL rewrites, sends OAuth/issuer metadata to port 8791,
sends MCP/protected-resource metadata to port 8790, and does not expose
`/internal/oauth/verify`. Operators must independently compare and qualify the exact generated
assets before any install or Caddy mutation.

The current `switchstand-edge-maintenance` preflight deliberately recognizes only the deployed
combined proxy set, with every upstream on port 8790. It will reject this split route set. That is a
useful activation fence: a reviewed split-aware replacement/rollback transaction and its readback
proof must land before production can adopt these generated assets.

Layer 2A still performs no state migration, service/Caddy installation, activation, or live
qualification. Layer 2B adds an inert offline state-copy tool; it does not control services,
install assets, mutate Caddy, or activate the split:

```console
switchstand-stable-auth-migrate copy --kind backup --source /absolute/state \
  --target /absolute/absent-backup --receipt /absolute/absent-backup.json \
  --lock-file /absolute/migration.lock
```

`migration` and explicit `restore` use the same absent-target transaction. The GitHub client secret
comes only from `SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET`; a receipt stores its fingerprint, never its
value. The tool holds a persistent no-follow lock, proves both supported writer units inactive
before and after copying, rejects links and unsupported FileTree metadata, relocates only FastMCP's
validated absolute collection-directory field, and records raw source/target plus root-normalized
logical checksums in a new mode-0600 receipt. A failed copy leaves its target as evidence and emits
no success receipt. The proof covers the repository-managed units, not unmanaged processes.

Rollback never silently rewinds OAuth state. `rollback-receipt` proves both writers remain inactive
and the supplied current raw tree digest remains stable across two reads before it records
`preserve-current-oauth-state`; restoring a backup is a separate explicit absent-target operation.
Disposable qualification proves a registered client and issued legacy
bearer survive a relocated encrypted-state copy under the same key. Until real-client activation
gates pass, the combined maintenance-window deployment above remains the production path.

Layer 3A adds an inert `switchstand-stable-auth-deploy activate` transaction for the first
combined-to-split cutover. It is not invoked by install, startup, or ordinary deployment. Its
preflight binds clean current and candidate Git SHAs, exact generated assets, a candidate-source
`PYTHONPATH`, separated secret-bearing environments, the running combined service and its OAuth
state/database identities, and the exact combined Caddy proxy set. It then:

1. installs and publicly proves a maintenance gate before stopping the combined process;
2. proves there are no durable effects with an `unknown` outcome, makes immutable backup and
   relocated OAuth-state copies while both possible state writers are stopped, installs the
   generated split units, and transfers reboot ownership from the combined unit to both split
   units with exact `is-enabled` readback;
3. starts stable auth first, proves the exact candidate process identity and a legacy bearer's
   full introspection identity, then starts and locally verifies the delegated edge;
4. replaces only the four exact Switchstand proxy handlers by stable Caddy IDs under the retained
   gate, preserving unrelated concurrent route additions, ungates only after exact readback, and
   runs an explicitly bound public candidate probe plus private-route non-exposure checks.

A definite failure before public exposure stops/removes the split units, restores the exact prior
proxy handlers and combined-unit reboot ownership, restarts and verifies the combined service, and
only then removes the gate. An
ambiguous mutation, rollback, interrupt, or post-exposure verification result returns `UNKNOWN`
and retains or reinstalls the gate for operator reconciliation. It never automatically restores
OAuth state; the old source remains untouched during this first cutover, and any possible
post-exposure writes require forward repair rather than a stale-state rewind. Receipts record the
last durable phase without secrets or exception text.

This layer does not yet supply the future edge-only split deployment transaction. Production
activation remains blocked until that Layer 3B path is independently reviewed and landed, the
exact generated/installed assets and copied-real-state rehearsal pass, and all live reliance gates
below are explicitly executed. Landing Layer 3A alone is inert and is not activation authority.

This topology targets one host and Switchstand may remain single-host indefinitely. Multi-host
readiness, clustering, active-active authorization, distributed locks, and a shared authorization
database are explicit non-goals, not deferred acceptance requirements for either Layer 2 or
production activation.

## Landing, activation, and client refresh

Inert landing evidence checks the pinned runtime imports, protected-resource and
OAuth metadata behavior, verified-principal mapping, exact executable tool
inventory, closed schemas, and grant/effect-gateway behavior. Ordinary startup
must remain unchanged.

Starting a host or changing the reverse proxy, OAuth application, tunnel,
database, provider, or trusted admission is separate activation work. Reliance
requires real identity and wrong-identity evidence, client discovery, an
authorized disposable effect with replay/readback, restart behavior, and clean
stop on the exact activated configuration. The edge gives admitted in-flight
HTTP calls up to 30 seconds to finish after shutdown begins; the service manager
must allow a longer stop window so resource cleanup can still complete. A
standalone Streamable HTTP GET is closed as a complete HTTP response during
shutdown. The client may reconnect that stream; after process replacement, an
expired in-memory session returns 404 and the client must initialize a new one.
An OAuth-continuity activation additionally uses a copied production OAuth store
to prove legacy access/refresh rebinding and restart persistence before the
single-process maintenance replacement. Rollback restores code but must not
rewind the OAuth store after any successful token rotation.

The stable-auth implementation may land only as an inert, independently reviewed change. Production
activation additionally requires real ChatGPT and Claude authorization and refresh evidence, live
GitHub correct-identity/wrong-identity/scope/revocation evidence, exact Caddy/private-introspection
routing and non-exposure evidence, copied-state migration and rollback rehearsal, and the ordinary
authenticated disposable effect/readback qualification. Until those gates pass, the maintenance
window above remains the only production replacement path.

An authenticated live `tools/list` proves the server-side inventory only.
Clients can retain bindings from before a client-visible schema or metadata
change. For every activation of such a change, Marco must reinstall the
ChatGPT app/connection; a server restart, authenticated `tools/list`, connection
refresh, or fresh chat without reinstall is insufficient. After reinstalling,
start a fresh chat and verify the exact schema exposed to that client and the
affected behavior before claiming the change is active. This is a standing
requirement for all future ChatGPT MCP work, not only initial launch.
