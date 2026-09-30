# Deferred zero-downtime authentication options

Status: research record, 2026-09-30. This document records a deferred choice; it is not an
implementation plan or activation authority.

## Current decision

Use the existing single-process
[maintenance-window replacement](chatgpt-mcp-edge.md#maintenance-window-replacement): install the
public `503 Retry-After` gate, stop the old edge, replace and verify it, then remove the gate. Agents
may retry bounded connection/read failures after the advertised delay. They must not blindly retry
writes or an `UNKNOWN` outcome.

Zero-downtime replacement is deferred, not rejected forever. It currently costs more than the brief,
explicit outage is worth and would add another authentication or coordination boundary while more
important work is pending.

Switchstand is expected to remain a **single-host** system and may never need multi-host operation.
That is a design constraint for any future Option C, not a temporary omission. Unless a concrete
future requirement explicitly reopens them, clustering, active-active authorization, distributed
locking, a shared authentication database, and multi-host failover/readiness are non-goals. A future
stable authorization service should survive replacement of the MCP edge on the same host without
pretending to be a distributed platform.

## Shortlist and estimates

These are directional implementation-plus-tests-and-docs estimates, not approved budgets.

| Route | Estimated retained change | Focused effort | Disposition |
|---|---:|---:|---|
| FastMCP + Descope | 570–1,230 lines; up to about 1,500 if custom GitHub verification is needed | 3–6 days | Best managed candidate |
| FastMCP + WorkOS AuthKit | 580–1,220 lines; up to about 1,400 with exact GitHub identity preservation | 3–6 days | Managed runner-up |
| Pomerium MCP gateway | 1,050–2,050 lines/config/tests/docs | 5–10 days | Best self-hosted candidate |
| Keycloak | 1,300–2,800 lines plus JVM/database operations | 1–3 weeks | Reject for now |
| Bespoke FastMCP auth sidecar | 3,500–7,000 lines | Multiple weeks | Reject for now |

Descope and WorkOS make a future stable authorization service materially smaller than a bespoke
sidecar, but neither turns it into a trivial change. Pomerium offers the clearest self-hosted gateway
boundary at greater implementation and operating cost. Keycloak currently lacks the required RFC
8707 resource-indicator support, and a bespoke authorization server would make Switchstand own a
security-critical protocol and state subsystem.

If Option C is reopened, the selected progression is deliberately narrower than those generic
product estimates:

1. Build a **disposable 150–300 LOC feasibility spike** against the best managed candidate. It proves
   only the exact identity, scope, refresh, and real-client boundaries below; it is not production
   code and creates no adoption presumption.
2. Only after a spike PASS, design and review a practical **single-host stable auth service** with an
   expected total retained implementation, tests, configuration, and documentation envelope of
   **900–1,800 LOC**.
3. Keep maintenance-window deployment as the production path until that separately authorized and
   qualified service is activated.

## Migration and unresolved proof

The managed and gateway options introduce a new issuer, signing keys, authorization metadata, and
client-registration relationship. They cannot safely import current FastMCP registrations, issued
tokens, or active sessions. Cutover therefore requires a deliberate ChatGPT reinstall/reconnect and
fresh-chat verification, plus Claude reconnect and verification. Do not copy tokens between issuers;
rollback after client cutover also normally requires reconnecting.

Before selecting any candidate, disposable qualification must prove:

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

## Revisit threshold

Reopen zero downtime only if at least one condition becomes true:

- authentication-related maintenance happens about monthly or more often and a 15–60 second bounded
  outage causes material work loss;
- multiple MCP resources or agent systems need shared authentication or policy;
- centralized audit, revocation, identity governance, or tool policy is independently required; or
- the observed incident/reconnect cost justifies at least 3–6 focused engineering days and a
  permanent external dependency.

If that threshold is crossed, the next research step is the bounded 150–300 LOC disposable spike,
expected to take about 0.5–1 day, not production adoption. It must test the exact GitHub
numeric-ID/scope claim, refresh replay, and both real clients before a design is selected. Multi-host
future-proofing is not part of that spike or the conditional service; only a new concrete requirement
can reopen the non-goals above.

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

**REJECTED FOR NOW**

- [Keycloak MCP authorization](https://www.keycloak.org/securing-apps/mcp-authz-server) — incomplete
  RFC 8707 support and disproportionate operations.
- [FastMCP full OAuth server](https://gofastmcp.com/servers/auth/full-oauth-server) and
  [OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy) as the stable boundary — either a
  bespoke security-critical service or continued embedded state, rather than the desired separation.
- Cloudflare Access/Auth0 for this decision — no clearer fit for the exact GitHub-ID, scope, and
  refresh requirements than the shortlisted first-party FastMCP integrations.

**REQUIRES LIVE PROOF**

- Exact GitHub numeric-ID/scope preservation, refresh replay/family revocation, and current
  ChatGPT/Claude behavior for the selected candidate.
- Seamless current-token/session migration; available evidence says to plan forced reconnect instead.
