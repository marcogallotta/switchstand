# Native certification profile

The native certification edge is a separate test-only deployment of the ordinary
HTTP/OAuth MCP. It must use a clean checkout pinned by
`SWITCHSTAND_CERTIFICATION_RUNTIME_SHA`, the exact `switchstand_test` database, a
separate OAuth application/resource path and `FASTMCP_HOME`, and the isolated
Asana project. It never replaces or reconfigures the production edge.

Launch the exact clean candidate with
`PYTHONPATH=$SWITCHSTAND_CERTIFICATION_REPO/src /home/marco/switchstand/.venv/bin/python -m switchstand.certification`.
Do not use an ambient console entry point. Supply certification-specific OAuth client
ID, secret and allowed GitHub user ID, resource URL
`https://laptop.tail46f0b9.ts.net:8446/mcp`, bind port,
repository path, runtime SHA, stable random ownership marker,
test-project GID, Asana token and database URL. Give the loopback edge a dedicated
tailnet-only Tailscale Serve HTTPS origin using
`tailscale serve --yes --bg --https=8446 http://127.0.0.1:8798`, with
exact `/mcp` resource and `/auth/callback`; register that exact callback in the
certification OAuth app. Before launch, read back `tailscale serve status --json`
and require the exact `8446` → `127.0.0.1:8798` mapping. The process rejects a
dirty/wrong checkout, production database aliases, the production port, mixed
project lineage and nonempty unowned test-project contents before serving.

Every created task receives the server-owned marker as an exact notes line. Startup
fully paginates the empty test project, deletes only valid marked residue, waits
up to ten seconds for deletion readback, and hard-fails on any unmarked task.
Send `SIGHUP` to the printed supervisor PID to restart only the edge while retaining
the fixture and database. Supervisor shutdown repeats cleanup and resets the database. A failure is reported as
`CLEANUP INCOMPLETE`; it is never a clean certification result, and the next run
always repeats pre-clean. The marker must remain stable across runs; rotation requires
exact manual cleanup with the old value before another run can start.

Launch through `scripts/switchstand-native-certification-client`; it preserves
normal Codex state but constructs a clean environment without `ASANA_TOKEN`,
database URLs or certification secrets. It disables production/managed/development
servers and enables only the disabled-by-default `switchstand_certification` alias,
whose tool surface and no-prompt policy exactly match ordinary Switchstand. Use the
same wrapper with `resume` for the resume proof.
OAuth-login that separate resource once. Exercise the ordinary tool surface,
creating the root fixture directly in the test project; do not remove its project
placement. Stop the supervisor after restart/resume so final cleanup can complete.
