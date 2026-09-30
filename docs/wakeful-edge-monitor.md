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

## Deferred activation and adapters

There is intentionally no systemd unit, timer, journal command, HTTP credential loader, event
delivery adapter, or Codex/Claude launcher in this slice. A later reviewed activation package must
provide those host bindings, retain cursor semantics, protect the fixed canary material, and prove
that a durable pending event is delivered once without an alert storm. Codex and Claude adapters
must consume the same neutral outbox contract; neither agent system belongs in the monitor.
