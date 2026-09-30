# Zero-downtime authentication decision record

Status: decision history and current direction, 2026-09-30. Maintenance-window replacement remains
deployed. Option C is reopened only for bounded inert single-host implementation; this document is
not activation authority.

## Current decision

Use the existing single-process
[maintenance-window replacement](chatgpt-mcp-edge.md#maintenance-window-replacement): install the
public `503 Retry-After` gate, stop the old edge, replace and verify it, then remove the gate. Agents
may retry bounded connection/read failures after the advertised delay. They must not blindly retry
writes or an `UNKNOWN` outcome.

This was originally deferred because it cost more than the brief explicit outage was worth and
would add another authentication or coordination boundary while more important work was pending.
That rationale remains the explanation for the deployed maintenance path; it is not erased by the
later reopening.

Switchstand is expected to remain a **single-host** system and may never need multi-host operation.
That is a design constraint for any future Option C, not a temporary omission. Unless a concrete
future requirement explicitly reopens them, clustering, active-active authorization, distributed
locking, a shared authentication database, and multi-host failover/readiness are non-goals. A future
stable authorization service should survive replacement of the MCP edge on the same host without
pretending to be a distributed platform.

## Later reopening and selected direction

Marco later explicitly reopened Option C, authorized building it if a sensible design emerged,
directed that work not wait, and clarified that Switchstand may remain single-host forever. The
corrected disposable spike then passed the current identity, resource, state-migration, revocation,
and private-boundary gates in [stable authorization split feasibility](auth-split-feasibility.md).

The selected bounded implementation is a **stable FastMCP authorization service with authenticated
private per-request introspection**. The stable service retains the current issuer, signing key,
encrypted OAuth/JTI state, canonical GitHub credential, and existing
`SwitchstandGitHubProvider.verify_token` path. A replaceable MCP edge delegates each bearer check to
that service over a filesystem-permissioned Unix socket or authenticated loopback endpoint and
accepts only the bounded verified principal. This preserves current per-request JTI and upstream
GitHub validation, including rejection after upstream revocation, while allowing the MCP application
process to be replaced independently.

The earlier "best managed candidate" condition is superseded for this bounded implementation by
Marco's later direction and the corrected local spike. Managed and gateway alternatives remain
useful research history and possible future replacements; they are not prerequisites for the
selected single-host implementation.

## Shortlist and estimates

These are directional implementation-plus-tests-and-docs estimates, not approved budgets.

| Route | Estimated retained change | Focused effort | Disposition |
|---|---:|---:|---|
| Stable FastMCP auth + private introspection | 900–1,800 lines/config/tests/docs | 4–8 days plus live qualification | Selected for inert single-host implementation |
| FastMCP + Descope | 570–1,230 lines; up to about 1,500 if custom GitHub verification is needed | 3–6 days | Prior best managed candidate; not selected |
| FastMCP + WorkOS AuthKit | 580–1,220 lines; up to about 1,400 with exact GitHub identity preservation | 3–6 days | Managed runner-up |
| Pomerium MCP gateway | 1,050–2,050 lines/config/tests/docs | 5–10 days | Best self-hosted candidate |
| Keycloak | 1,300–2,800 lines plus JVM/database operations | 1–3 weeks | Reject for now |
| Bespoke new OAuth implementation | 3,500–7,000 lines | Multiple weeks | Reject for now |

Descope and WorkOS make a future stable authorization service materially smaller than a bespoke
sidecar, but neither turns it into a trivial change. Pomerium offers the clearest self-hosted gateway
boundary at greater implementation and operating cost. Keycloak currently lacks the required RFC
8707 resource-indicator support, and a bespoke authorization server would make Switchstand own a
security-critical protocol and state subsystem.

The reopened progression remains deliberately narrower than the generic product estimates:

1. The disposable feasibility spike was built against the existing FastMCP provider seam rather
   than adopting a managed candidate. Its corrected form proved private per-request validation,
   current encrypted-state/existing-token migration after stopping the old writer, and immediate
   upstream-revocation rejection. It is inert evidence, not production code.
2. After that spike PASS, design and independently review a practical **single-host stable auth
   service** with an
   expected total retained implementation, tests, configuration, and documentation envelope of
   **900–1,800 LOC**.
3. Keep maintenance-window deployment as the production path until the implementation passes real
   ChatGPT, Claude, GitHub, Caddy/private-socket, copied-state migration, rollback, and ordinary
   activation qualification.

## Migration and unresolved proof

The selected local split retains the current issuer, GitHub application, signing derivation, and
encrypted FastMCP state. The inert spike proved that, after stopping the old writer, the same client
registration and an already-issued token remain valid through the stable provider and private
verification boundary. It does not prove live-client sessions, refresh during cutover, or production
file copying; those remain activation gates. There must never be concurrent old and new writers to
the file-backed OAuth state.

The managed and gateway alternatives would introduce a new issuer, signing keys, authorization
metadata, and client-registration relationship. Their forced-reconnect consequences remain relevant
if a future decision selects one of them, but they no longer describe the selected local split.

Before production activation, exact qualification of the selected implementation must prove:

- the allowed identity remains the immutable GitHub numeric user ID, not username or email;
- the current GitHub scope requirement is preserved rather than weakened by identity enrollment;
- refresh rotation, used-token replay/family revocation, concurrent refresh, and revocation behavior;
- current ChatGPT and Claude CIMD/DCR, resource, audience, and refresh interoperability; and
- outage, signing-key rotation, rollback, and redacted observability behavior.

Published material did not settle Descope's exact GitHub numeric-ID token mapping or inbound-client
refresh replay/family-revocation behavior. WorkOS documentation did not establish preservation of
the current numeric-ID-plus-scope trust claim. Pomerium's documented stable GitHub identity depends
on a release newer than the production release assessed. Those are qualification unknowns, not
assumptions an implementation may silently accept.

## Prior revisit threshold and supersession

The original deferral said to reopen only if at least one condition became true:

- authentication-related maintenance happens about monthly or more often and a 15–60 second bounded
  outage causes material work loss;
- multiple MCP resources or agent systems need shared authentication or policy;
- centralized audit, revocation, identity governance, or tool policy is independently required; or
- the observed incident/reconnect cost justifies at least 3–6 focused engineering days and a
  permanent external dependency.

Marco's later explicit direction superseded that conditional threshold for **inert single-host
implementation**, and the corrected spike is now complete. It did not authorize production
activation or reopen any multi-host non-goal. Real-client refresh and cutover behavior remain
qualification gates before reliance.

## Source dispositions

**USED**

- [MCP authorization specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
  and [RFC 8707](https://www.rfc-editor.org/info/rfc8707/) — required authorization, resource, and
  audience behavior.
- [FastMCP remote OAuth](https://gofastmcp.com/servers/auth/remote-oauth),
  [Descope provider](https://gofastmcp.com/python-sdk/fastmcp-server-auth-providers-descope), and
  [WorkOS provider](https://gofastmcp.com/python-sdk/fastmcp-server-auth-providers-workos) — supported
  resource-server integration seams.
- [Descope MCP](https://docs.descope.com/mcp),
  [Descope inbound authorization server](https://docs.descope.com/identity-federation/inbound-apps/authorization-server),
  and [Descope GitHub provider](https://docs.descope.com/auth-methods/oauth/providers/setting-up-your-own-apps/github)
  — managed MCP and upstream GitHub capabilities; numeric-ID/replay details remain unproved.
- [WorkOS MCP](https://workos.com/docs/authkit/mcp) and
  [AuthKit identity linking](https://workos.com/docs/authkit/identity-linking) — managed MCP support
  and the identity-model tradeoff.
- [Pomerium MCP](https://www.pomerium.com/docs/capabilities/mcp/protect-mcp-server),
  [GitHub IdP](https://www.pomerium.com/docs/integrations/user-identity/github), and
  [data storage](https://www.pomerium.com/docs/internals/data-storage) — gateway, identity, and
  operating requirements.
- [GitHub OAuth authorization](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps),
  [best practices](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/best-practices-for-creating-an-oauth-app),
  and [scopes](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)
  — current upstream identity and scope constraints.

**REJECTED OR NOT SELECTED**

- [Keycloak MCP authorization](https://www.keycloak.org/securing-apps/mcp-authz-server) — incomplete
  RFC 8707 support and disproportionate operations.
- [FastMCP full OAuth server](https://gofastmcp.com/servers/auth/full-oauth-server) — a bespoke
  protocol implementation remains rejected. The existing
  [OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy) is retained inside the selected stable
  auth service; embedding it in each replaceable edge remains rejected.
- Cloudflare Access/Auth0 for this decision — no clearer fit for the exact GitHub-ID, scope, and
  refresh requirements than the shortlisted first-party FastMCP integrations.

**REQUIRES LIVE PROOF**

- Real ChatGPT and Claude authorization, refresh, reconnect, and session behavior against the split
  service.
- Live GitHub callback, correct/wrong identity, scope, and revocation behavior; exact Caddy/private
  socket non-exposure; production copied-state migration, rollback, and recovery behavior.

Layer 2B supplies inert single-host offline copy/checksum and rollback-receipt tooling plus a
disposable legacy-token continuity proof. It does not change the deployed maintenance-window path;
production rehearsal and every real-client gate above remain activation requirements.
