# ADR 0004: Project the native tree through a read-only flight board

- Status: Accepted
- Date: 2026-08-27

## Context

Stage A validated the App Server tree protocol without replacing the synthetic two-role UI.
Operators now need the real root/descendant topology and runtime truth without adding messaging,
lifecycle control, transcripts, or a second durable state model.

## Decision

When launched with one exact root id, Switchstand replaces the primary browser surface with a
read-only native flight board. A restricted observer reuses the Stage A tree validator, forces
state-database-only descendant listing, reconnects per pass, and publishes only a privacy-bounded
run-local projection after a complete pass succeeds.

Polling results are completed multi-request observation passes and endpoint differences, not
atomic snapshots or native events. Observed-active duration advances only across consecutive
successful passes and resets on every gap, missing thread, or non-active result.

## Consequences

The old engine remains an available reliability spike. Native mode has no POST controls and no
persistence. It can miss intermediate transitions, and a failed pass leaves a clearly historical
last-complete view. A real exact-head socket run remains required for the live checkpoint.
