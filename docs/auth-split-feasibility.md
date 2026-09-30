# Stable authorization split feasibility

Status: inert single-host feasibility spike. This is not an activation or deployment procedure.

## Question and boundaries

The spike asks whether the current FastMCP GitHub OAuth proxy can remain at Switchstand's current
public issuer while the replaceable MCP edge becomes a separate JWT-validating resource server.
It uses disposable state and no external credentials or services.

Non-goals are multi-host operation, clustering, active-active authorization, distributed locking,
a shared authorization database, live-client qualification, provider writes, and deployment.

## PASS gates

The executable spike must establish all of these against the pinned FastMCP runtime:

1. Authorization routes can be mounted without the MCP endpoint, retain the current issuer, and
   advertise authorization-code/S256 PKCE, DCR, and CIMD.
2. A separate `RemoteAuthProvider` publishes RFC 9728 metadata for the exact external `/mcp`
   resource and names the stable authorization issuer.
3. The proxy-issued access token has the exact RFC 8707 audience and a separate verifier rejects a
   wrong audience or missing scope.
4. The signed token carries the already-validated immutable GitHub numeric ID and upstream scope;
   the edge rejects a different identity.
5. With writers stopped, the default encrypted state and a new-format issued token remain usable
   after creating a replacement provider with the same storage root and signing key. A different
   key cannot read the registration or validate the token.

`tests/test_auth_split_feasibility.py` is the disposable proof. It deliberately exercises FastMCP's
real route builders, authorization-code exchange, token issuer, encrypted default storage, JWT
verifier, and remote-resource metadata rather than reproducing those contracts in mocks. Only the
GitHub network response is replaced with a local validated identity.

## Result and limitation

The split is feasible on one host if all gates pass. The migration unit is the encrypted FastMCP
state **plus the exact signing key and public issuer/resource configuration**. There must never be
concurrent old and new writers to that state: stop, snapshot, start the stable authorization
service, verify, then switch the resource route.

Client registrations and encrypted upstream state are restorable, but current production access
tokens predate the signed `upstream_claims` identity used by the split verifier. Although their
signature and audience remain valid, a JWT-only edge cannot establish their GitHub subject and must
reject them. Production cutover therefore requires client reauthorization, or a separately designed
temporary introspection bridge. This spike does not establish token-transparent migration.

FastMCP's OAuth proxy currently issues HS256 tokens. The resource edge therefore receives the same
HMAC key used to sign tokens. The split creates an operationally stable authorization lifecycle,
but not a cryptographic trust boundary: a compromised edge that has the HMAC key could forge a
token. This is acceptable only if the authorization and edge processes remain within the same
single-host trust boundary. A future stronger boundary requires an asymmetric issuer/JWKS feature
or a different authorization server; it is not part of this spike.

This proof does not establish ChatGPT or Claude behavior, live GitHub callbacks, refresh under a
real upstream credential, Caddy path routing, service supervision, backup/restore operations, or
client continuity during production cutover. Those remain activation qualification, not implied by
an inert PASS.
