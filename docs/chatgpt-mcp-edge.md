# ChatGPT MCP edge

This is Switchstand's authenticated HTTP/OAuth edge for ordinary ChatGPT and
Codex clients. It is distinct from the launcher-bound managed Codex STDIO
surface and from the local development MCP. Landing edge code or documentation
does not start a listener, configure OAuth, issue a workspace grant, or connect
a client.

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

Raw `source_*` tools are not part of the ordinary current surface. They remain
available only on bounded managed/recovery compatibility routes described in
[MCP work, history, and compatibility](source-history-feedback.md). New ordinary
flows use provider-neutral WorkIds and work/history/event tools.

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
must allow a longer stop window so resource cleanup can still complete.

An authenticated live `tools/list` proves the server-side inventory only.
Clients can retain bindings from before a client-visible schema or metadata
change. For every activation of such a change, Marco must reinstall the
ChatGPT app/connection; a server restart, authenticated `tools/list`, connection
refresh, or fresh chat without reinstall is insufficient. After reinstalling,
start a fresh chat and verify the exact schema exposed to that client and the
affected behavior before claiming the change is active. This is a standing
requirement for all future ChatGPT MCP work, not only initial launch.
