# Stable authorization split feasibility

Status: inert single-host feasibility spike. This is not an activation or deployment procedure.

## Question and boundaries

The spike asks whether the current FastMCP GitHub OAuth provider can remain at Switchstand's public
issuer while the replaceable MCP edge delegates token verification to that stable service. It uses
disposable state and no external credentials or services.

Non-goals are multi-host operation, clustering, active-active authorization, distributed locking,
a shared authorization database, live-client qualification, provider writes, and deployment.

## Selected invariant and PASS gates

The stable authorization service remains the only owner of the FastMCP signing key, encrypted OAuth
state, JTI mappings, upstream GitHub token, and GitHub verifier. Every MCP bearer-token check calls a
private authenticated loopback/Unix-socket endpoint which invokes the existing
`SwitchstandGitHubProvider.verify_token`. The edge accepts only the resulting bounded principal.

The executable spike establishes these gates against the pinned FastMCP runtime:

1. Authorization routes can be mounted without the MCP endpoint, retain the current issuer, and
   advertise authorization-code/S256 PKCE, DCR, and CIMD.
2. A separate `RemoteAuthProvider` publishes RFC 9728 metadata for the exact external `/mcp`
   resource and names the stable authorization issuer.
3. The private verifier returns the exact issuer, resource, client, scope, and immutable GitHub
   numeric subject established by the current provider path. The edge owns no signing key.
4. A missing/invalid internal credential, malformed reply, invalid bearer token, internal error, or
   unavailable auth service fails closed.
5. After an upstream token becomes invalid, the next edge verification rejects the otherwise
   unexpired FastMCP token. This preserves current per-request JTI/upstream GitHub validation.
6. With writers stopped, default encrypted state, client registration, and an existing token remain
   usable after creating a replacement auth provider with the same state root and GitHub secret. A
   different secret selects different encrypted state and cannot recover the registration.

`tests/test_auth_split_feasibility.py` exercises FastMCP's route builders, authorization-code
exchange, encrypted default storage, current Switchstand provider validation, remote-resource
metadata, and a hermetic HTTP boundary. Only the external GitHub response is replaced locally.

## Result and availability tradeoff

The bounded introspection split is feasible on one host. The migration unit is the encrypted
FastMCP state plus the exact GitHub secret and public issuer/resource configuration. There must be
no concurrent writers: stop the old process, snapshot, start the stable authorization service,
verify an existing token, and only then switch the MCP resource route.

The production private protocol should use a filesystem-permissioned Unix socket plus a separately
stored bearer secret, a small response limit, a short timeout, no proxy/environment inheritance,
and no token logging. Caddy and Tailscale must never publish the private endpoint.

Unlike offline JWT verification, every MCP request now depends on the stable authorization service
and GitHub verification. An auth-service failure therefore makes the edge fail closed even while a
client token is unexpired. That is the price of preserving immediate upstream revocation with the
current provider. On one host the auth service, edge, database, Caddy, and tunnel already share a
machine failure domain; supervision, a readiness probe, and a stable process are proportionate.

The rejected alternative is short-lived self-contained JWTs checked locally by the edge, with
GitHub revalidation only at refresh. It permits revoked upstream access until expiry and relies on
ChatGPT and Claude refreshing reliably. Their real refresh behavior cannot be established by this
credential-free inert spike, so that route is not selected. It may be reconsidered only after
exact-client qualification and an explicitly accepted revocation window.

This proof does not establish ChatGPT or Claude behavior, live GitHub callbacks, Caddy/systemd/Unix
socket wiring, backup/restore operations, or production cutover. Those remain activation
qualification, not implied by an inert PASS.
