# Wakeful persistence prototype

This inert prototype supplies an agent-system-neutral, sanitized event envelope and a local SQLite
store for journal cursors, monitor transition state, bounded single-cycle leases, and a durable
delivery outbox. Landing it does not start monitoring, read a journal, call a service, or wake an
agent.

One monitor cycle acquires a durable lease before reading its cursor and holds ownership through
the eventual atomic cursor, transition, and outbox commit. Another process cannot acquire that
monitor concurrently. A lease survives process restart, expires after a bounded interval, and
rejects its stale former owner after takeover. Repeated equal failure states are suppressed;
recovery is emitted only after two consecutive healthy commits.

The stored event contains fixed schema, identity, time, source, subject, kind, severity, and summary
fields. Probe payloads, raw journal lines, credentials, authorization material, and principal data
do not belong in the envelope. Dispatch to Codex, Claude, or another consumer is deferred to a
dependent slice and must consume the same outbox rather than changing this contract.
