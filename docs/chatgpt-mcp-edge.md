# ChatGPT MCP edge

This is the default-off authenticated ChatGPT edge for task
`1218470091228623`. Landing it does not start a listener, create a grant, or
connect ChatGPT. It adds only an ASGI application factory.

The edge exposes the existing ChatGPT MCP product surface: `grant_get`,
`work_get`, `source_task`, `source_stories`, `source_story`, and `work_append`.
It does not create a second write path. `work_append` runs through the existing
current `WorkGrant`, exact active `WorkId`, grant version, observed source
revision, append qualification, durable operation identity, UNKNOWN handling,
and exact provider-effect readback. Trusted grant issuance is not an MCP tool.

## Private configuration

The future authorized host must keep these values outside the repository and
logs:

```text
SWITCHSTAND_MCP_GITHUB_CLIENT_ID=<dedicated GitHub OAuth app client ID>
SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET=<dedicated GitHub OAuth app secret>
SWITCHSTAND_MCP_GITHUB_USER_ID=<allowed immutable numeric GitHub user ID>
SWITCHSTAND_MCP_RESOURCE_URL=https://public.example/switchstand/mcp
SWITCHSTAND_MCP_BIND_HOST=127.0.0.1
SWITCHSTAND_MCP_BIND_PORT=8790
DATABASE_URL=<existing current-schema Switchstand database>
ASANA_TOKEN=<provider credential allowed by the issued grant>
```

The public resource URL must use HTTPS and end exactly in `/mcp`; the process
refuses a non-loopback bind. Configure the GitHub OAuth callback at
`<resource-base>/auth/callback`. A reverse proxy must route `/mcp`, OAuth
authorize/token/register/callback/consent paths, and both OAuth well-known
metadata paths to the loopback process. The OAuth request requires `read:user`
only to establish GitHub identity; Switchstand's grant remains the authority
for every work read or write.

FastMCP owns encrypted OAuth client registrations and token mappings in its
user-data directory (normally `~/.local/share/fastmcp`). This is transport
session state, not Switchstand application authority or a second grant store.
Losing it invalidates sessions and requires clients to reconnect.

## Landing and activation evidence

For inert landing, prove the pinned FastMCP 4.0.3/MCP 2.1.1 pair imports on
Python 3.14, unauthenticated requests receive the protected-resource challenge,
metadata advertises the exact resource and S256 PKCE, the verified token maps
to the existing principal, exact discovery includes the six tools above, and
an authenticated write is admitted or denied by the existing grant/effect
gateway with idempotent replay. Ordinary startup must remain unchanged.

Starting a host or changing the reverse proxy, OAuth app, tunnel, database, or
provider is activation work. Before relying on it, separately verify the real
GitHub identity and wrong-identity rejection, real ChatGPT discovery, one
authorized disposable write plus replay, restart readback, and clean stop.
