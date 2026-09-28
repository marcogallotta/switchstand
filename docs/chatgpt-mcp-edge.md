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

## Landing, activation, and client refresh

Inert landing evidence checks the pinned runtime imports, protected-resource and
OAuth metadata behavior, verified-principal mapping, exact executable tool
inventory, closed schemas, and grant/effect-gateway behavior. Ordinary startup
must remain unchanged.

Starting a host or changing the reverse proxy, OAuth application, tunnel,
database, provider, or trusted admission is separate activation work. Reliance
requires real identity and wrong-identity evidence, client discovery, an
authorized disposable effect with replay/readback, restart behavior, and clean
stop on the exact activated configuration.

An authenticated live `tools/list` proves the server-side inventory only.
Clients can retain bindings from a session created before a schema change. Start
a fresh client session and verify the tools exposed to that client before
claiming a newly added or changed capability is usable there.
