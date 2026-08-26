# ADR 0001: A narrow flat-file slice with a real Codex adapter

- Status: Accepted
- Date: 2026-08-26

## Context

The first Switchstand checkpoint needs to test one operator interaction seam: direct access to
two durable logical roles while stops, corrections, replacement, restart, and late results stay
truthful. Broad orchestration or production infrastructure would make that seam harder to
inspect before its behavior is proven.

## Decision

Build exactly one local Work with two fixed durable roles. Persist one atomic JSON snapshot and
one JSONL transition log. Serve a vanilla HTML/CSS/JavaScript UI from a Python standard-library
HTTP process. Use a small but real Codex app-server v2 adapter over its Unix WebSocket socket;
use fakes only in tests.

Flat files keep the state and failure cases legible, require no service dependency, and are
enough for a single-process prototype. The real adapter prevents the core design from being
validated only against an invented transport, while remaining explicit about the fact that
mock protocol tests are not a live checkpoint. The narrow role and Work boundary keeps effort
on ordered delivery, restart reconciliation, exact controls, and generation/attempt fencing.

## Consequences

The repository runs without third-party runtime dependencies or a frontend build. State can be
inspected directly and tests can cover every transition with a small fake adapter.

The JSON snapshot and JSONL event are not one transaction, multiple writers are unsupported,
schema migration is absent, and the browser service is local-only and unauthenticated. Role
count, additional Works, databases, generalized providers, remote lifecycle integration, and
production operations remain deferred until evidence from this slice justifies them.
