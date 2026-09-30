# Draft RCA: ChatGPT MCP OAuth failure, 2026-09-30

Status: draft; mitigation and final causal confirmation are still open. Times below
are Europe/Rome.

## Impact and current evidence

Existing ChatGPT sessions could not reliably use Switchstand. The edge process remained
running and many `/mcp` requests returned HTTP 200, but current journal evidence around
16:11–16:13 showed `invalid_token` responses, upstream refresh failure,
`bad_refresh_token`, and `/token` HTTP 401. Transport availability therefore concealed
an authentication-path outage.

The strongest current causal explanation is upstream credential proliferation. The
FastMCP store contained 23 GitHub token lineages for the same single-user OAuth path.
[GitHub documents](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#creating-multiple-tokens-for-oauth-apps)
a limit of ten tokens per user/application/scope combination and revokes an existing
token when that limit is exceeded. That makes revocation of older session credentials
expected. GitHub security-log or equivalent provider readback is still required to
confirm the incident-specific revocation reason.

A second defect is plausible but not yet established as an incident cause: FastMCP
4.0.3's downstream refresh exchange can race two uses of a rotating refresh token.
Its transparent access-token refresh has an in-process lock, but the downstream token
exchange is a read/refresh/write sequence without equivalent durable serialization.
The observed provider error alone does not distinguish that race from quota revocation.

This was not introduced by a recent FastMCP version bump: 4.0.3 was pinned when the
HTTP edge was added. A later change extended FastMCP-issued access-token lifetime,
making transparent upstream refresh more important; rolling it back would not restore
revoked GitHub credentials or reduce token lineage count.

A September 16 `NetworkSettings.IPAddress` startup trace was historical and described
an already-corrected launcher defect. It was not evidence for this incident.

## Why it escaped

- Edge and process tests replace real OAuth verification with a fixed bearer token and
  use in-memory FastMCP storage. They do not exercise upstream authorization, GitHub's
  token quota, rotating refresh tokens, multiple clients, or persisted OAuth restart.
- The access-token-lifetime test asserts the configured duration, not expiry/refresh
  behavior.
- The activation doctor checks an unauthenticated 401 challenge and OAuth metadata. It
  cannot detect a revoked credential or failed authenticated tool call.
- Acceptance exercised a fresh client, not more than ten clients, an old existing
  client, or a forced upstream refresh boundary.

The incident response also delayed diagnosis. Broad delegation preceded inspection of
the current journal, the requested log command/path was not supplied immediately, and
historical and current failures were briefly conflated. Long silent intervals during
an explicitly urgent exchange removed operator visibility. These are process failures,
not evidence about which person or tool caused the software defect.

## Clearing conditions

The OAuth fix must prove that adding downstream clients does not keep minting upstream
GitHub credentials. Tests must cover more than ten clients, simultaneous refresh,
duplicate downstream refresh, persisted restart, client isolation, copied-real-state
migration, and crash recovery. Activation must preserve the OAuth store, validate the
selected upstream credential, and prove authenticated reads from both an old affected
client and a new client across forced refresh and restart. HTTP 200 or metadata alone
cannot establish recovery.

Monitoring follow-up must add an authenticated canary and bounded signals for upstream
lineage count, token issuance, refresh outcome, dangling mappings, provider reads, and
crash loops. Wakeful should consume state transitions such as first authenticated
failure and recovery, deduplicate them, wake the designated Coordinator, and retain
delivery/acknowledgement evidence. That integration remains unimplemented and must not
be treated as current coverage.
