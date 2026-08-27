# ADR 0005: Add one exact-turn native emergency stop

- Status: Accepted
- Date: 2026-08-27
- Authority: GitHub issue #12 and root decisions 5442493928 / 5442566168

## Context

The B1 flight board observes native work but cannot request cancellation. The original itemless
turn-list gate failed on App Server 0.149.0 and 0.150.1. The accepted fallback is stable
`thread/read(includeTurns=true)`, which reads without resume or subscription but transiently
brings transcript content through local memory.

## Decision

Add one native-only two-step control. Prepare accepts only a current run-local agent reference,
requires connected active board evidence, performs a byte-capped one-shot read, strictly
projects the sole active turn, and returns an opaque expiring single-use confirmation reference.
Commit consumes it before I/O, repeats the bounded projection, requires the same exact turn, and
sends one interrupt without retry or retargeting.

Only fixed codes, safe references, and truthful `not_sent`, `rejected`, `requested`, `confirmed`,
`not_confirmed`, or `unknown` outcomes reach the browser. Full read responses are discarded
immediately and never logged, persisted, copied to errors/evidence, or retained in receipts.
Native controls require JSON, a custom header, and same-origin loopback Host/Origin; native mode
rejects non-loopback binding and emits no permissive CORS.

## Consequences

Cancellation is a request for one turn, not rollback or recursive process control. Receipts and
tombstones are capped, expiring, process-local state and reset on restart. The boundary does not
authenticate same-user local processes or browser automation. Persistence, remote control,
stronger authentication, retries, and transcript UI or telemetry remain out of scope.
