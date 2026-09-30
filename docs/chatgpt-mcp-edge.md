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
- the fixed agent-project bootstrap operator utility, with preview by default;
- work discovery, exact legacy-reference resolution, reads, structure, history,
  attachments, and events;
- grant-checked work creation, update, relation, and append operations;
- durable work-addressed and registered-agent messaging; and
- required-result persistence.

The executable inventory and client policy are owned by
`build_ordinary_tools` in `src/switchstand/chatgpt_mcp.py`, its edge tests, and
the repository's `.codex/config.toml`. Do not copy the full tool list into prose:
it changes as capabilities are added or retired. `repository_bundle_get` is the
bootstrap route for an ordinary ChatGPT session without a normal checkout; a
Codex session that already has the repository uses Git normally.

The edge does not create a second authority system. It resolves the verified
OAuth principal to the current workspace admission. Work tools apply the
existing WorkId, grant-version, revision, operation-identity, UNKNOWN, and
effect-readback rules. The agent-project bootstrap instead preserves the
operator utility's narrower contract, including an operator-inspected UNKNOWN
after a possible write. Trusted grant issuance is not an MCP tool. Provider
credentials and grant state stay behind the service boundary.

`effect_reconcile` is the provider-neutral recovery route for an already-prepared UNKNOWN scalar
update. It accepts only the durable OperationId, reconstructs no caller-supplied mutation,
and returns a sanitized intent/readback projection. A mismatch remains UNKNOWN and continues to
block the work; the ordinary edge exposes no retirement or release operation. Only a scalar-update
blocker points callers to this tool. Relation and append blockers have no ordinary exact-recovery
route and require trusted operator adjudication without a resend.

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
public resource URL, bind settings, database URL, and provider credentials
outside the repository and logs. The public resource URL uses HTTPS and ends in
`/mcp`; the process binds only to loopback behind its reverse proxy. OAuth
metadata, authorization, callback, consent, registration, token, and MCP routes
must reach that same process. OAuth establishes client identity; Switchstand's
current workspace admission remains the authority for each read or effect.

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
rotation is serialized within the one edge process. Running overlapping edge
processes against this file-backed OAuth store is not a supported activation shape:
its refresh locks are process-local.

The read-only edge doctor needs no database or provider credentials. Keep its
three protected OAuth values and the non-secret probe configuration in one
mode-`0600` environment file:

```dotenv
SWITCHSTAND_MCP_RESOURCE_URL=https://public.example/switchstand/mcp
SWITCHSTAND_MCP_BIND_HOST=127.0.0.1
SWITCHSTAND_MCP_BIND_PORT=8790
SWITCHSTAND_MCP_PUBLIC_URL=https://public.example/switchstand/mcp
```

Then the canonical activation check is:

```console
scripts/switchstand-edge-doctor --env-file /path/to/edge-doctor.env \
  --expected-sha "$EXPECTED_SHA" --repo /path/to/switchstand
```

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
metadata path, and only then stops the edge. While offline it snapshots the exact
FastMCP state directory without parsing or logging its secret contents, atomically
swaps the launcher, starts the edge, runs the edge doctor locally, removes the
gate, and runs the public doctor. A definite failure rolls back according to the
last completed phase: after a launcher swap this includes stopping the candidate,
restoring the launcher and verifying the old edge against the current OAuth state
before ungating. It never restores the OAuth snapshot automatically because doing
so could discard registrations or token rotations accepted after the snapshot.
Snapshot restoration is a separate offline corruption-recovery action with explicit
session-loss consequences. Ambiguous mutation/readback or ambiguous rollback returns `UNKNOWN`
and retains or reinstalls the maintenance route. An interrupt after gate insertion follows the
same fail-closed path. Never blindly rerun an UNKNOWN;
inspect its receipt and live gate/service/launcher state first.

The retained snapshot, failed candidate state (when applicable), launcher backup,
and receipt stay in the attempt directory as recovery evidence. The tool does not
prepare candidates, launchers, authorization, client reinstall, or activation
approval. Landing it does not change the running service or Caddy configuration.
This single-runtime maintenance path remains the **current deployed production model**.
Agent-visible behavior during the gate is owned by
[live-incident operations](operations-live-incident.md#known-edge-maintenance-window).

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

The delegated-edge file contains the resource URL, numeric GitHub user ID, database and provider
configuration, but **not** the GitHub client secret, signing material, or `FASTMCP_HOME`:

```dotenv
SWITCHSTAND_MCP_GITHUB_USER_ID=192548
SWITCHSTAND_MCP_RESOURCE_URL=https://public.example/switchstand/mcp
DATABASE_URL=...
ASANA_TOKEN=...
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

The file must be owned by the service user, regular, symlink-free, and exactly mode `0600`.
Rotation is deliberately offline: stop both split processes, rotate with the last readback digest,
read back the new digest, then start stable auth before the delegated edge. The processes read the
credential once at startup; rotating under running processes would temporarily split their trust
state.

Generate candidates into a new empty directory; this does not install or apply them:

```console
switchstand-stable-auth-host render \
  --runtime-python /absolute/runtime/bin/python \
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
qualification. Layer 2B owns the exclusive-writer state migration doctor, copied-state checksums,
legacy-token continuity, backup/restore, and rollback receipts. Until that separately reviewed
layer and the real-client activation gates pass, the combined maintenance-window deployment above
remains the production path.

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
GitHub correct-identity/wrong-identity/scope/revocation evidence, exact Caddy/private-socket routing
and non-exposure evidence, copied-state migration and rollback rehearsal, and the ordinary
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
