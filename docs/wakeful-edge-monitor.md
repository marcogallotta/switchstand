# Wakeful edge-monitor prototype

This prototype establishes two inert slices: a neutral sanitized wake-event contract with a local
SQLite outbox/cursor, and an independent one-cycle classifier for the authenticated Switchstand
edge. Landing these modules does not start monitoring, read the host journal, call the live edge,
or wake an agent.

## Behavioral boundary

The edge monitor accepts injected seams for systemd state, journal records after an opaque durable
cursor, ordinary HTTP behavior, and one authenticated functional read. It distinguishes:

- transport failure: no active process/PID or HTTP transport;
- OAuth failure: new journal evidence contains a bounded refresh-failure marker, even if the
  process is live and ordinary HTTP returns 200;
- provider failure: the authenticated read returns the provider-error class;
- functional failure: HTTP semantics or the authenticated read are otherwise wrong; and
- missing capability: no fixed canary was configured.

An expected unauthenticated 401 challenge is healthy transport and does not alert. Historical
journal records are excluded by the injected journal reader read-after-cursor contract. Raw
journal text and probe responses are never written to SQLite; only fixed classifications and
summaries enter the outbox. Repeated identical failures are suppressed, and recovery is emitted
after two consecutive healthy cycles.

A SQLite-backed single-cycle lease covers cursor acquisition, journal read, classification, and
the atomic cursor/state/outbox commit. A concurrent cycle does no probing or writing. The lease
survives a monitor-process restart, expires after a bounded interval, and rejects a stale owner
after takeover so an older cycle cannot regress the cursor or duplicate a transition.

The authenticated canary must be a pre-registered, read-only identity bound to one fixed WorkId.
The monitor never registers a client, authorizes OAuth, refreshes a token, searches for a target,
or changes work. If that exact capability is absent, it records missing-capability rather than
pretending the functional path passed.

## Inert host qualification

`python -m switchstand.edge_monitor_host` binds one monitor cycle to fixed-argument `systemctl` and
`journalctl` reads, local/public OAuth challenge and metadata checks, and an optional fixed
read-only `work_get` canary. The bearer token is accepted only through a mode-0600 file and is
never persisted or printed. It is read only after the functional target is proven to be either the
exact HTTPS resource/public endpoint or the configured loopback HTTP `/mcp` endpoint. A successful
canary also requires the returned item ID to equal the fixed WorkId. Missing or invalid capability
material produces `missing_capability`.

The command remains inert: it installs no unit or timer, creates no credentials, performs no OAuth
registration/authorization/refresh, and dispatches no event. `events` reads the sanitized pending
outbox; `demo` exercises failure deduplication and two-pass recovery in a newly created private
state directory by default:

```sh
PYTHONPATH=src python -m switchstand.edge_monitor_host demo
PYTHONPATH=src python -m switchstand.edge_monitor_host --state-dir /path/from/output events
PYTHONPATH=src python -m switchstand.edge_monitor_host --state-dir /private/state check --fixture healthy
PYTHONPATH=src python -m switchstand.edge_monitor_host --state-dir /private/state check --fixture bad_refresh_token
```

For a real one-shot check, add the required `--local-url` and `--resource-url`, optionally
`--public-url`,
`--bearer-token-file`, and the exact harmless `--work-id`. Omit the last two to safely demonstrate
`missing_capability` without creating credentials.

## Deferred activation and adapters

There is intentionally no systemd unit, timer, event delivery adapter, or Codex/Claude launcher.
A later reviewed activation package must schedule the one-shot runner, protect/provision the fixed
canary material, and prove that a durable pending event is delivered once without an alert storm.
Codex and Claude adapters must consume the same neutral outbox contract; neither belongs in the
monitor.
