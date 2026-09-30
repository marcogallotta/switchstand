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

This single-runtime maintenance path is the selected deployment model. Zero-downtime overlap is
deferred: a future design may evaluate a conventional identity-aware proxy or a dedicated
FastMCP-auth boundary, but neither is an active prerequisite and deployment work must not grow an
in-repository OAuth sidecar or cross-process token protocol speculatively. Agent-visible behavior
during the gate is owned by
[live-incident operations](operations-live-incident.md#known-edge-maintenance-window).

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

An authenticated live `tools/list` proves the server-side inventory only.
Clients can retain bindings from before a client-visible schema or metadata
change. For every activation of such a change, Marco must reinstall the
ChatGPT app/connection; a server restart, authenticated `tools/list`, connection
refresh, or fresh chat without reinstall is insufficient. After reinstalling,
start a fresh chat and verify the exact schema exposed to that client and the
affected behavior before claiming the change is active. This is a standing
requirement for all future ChatGPT MCP work, not only initial launch.
