# ADR 0003: Keep the first code-quality gate small and deterministic

- Status: Accepted
- Date: 2026-08-27

## Context

Switchstand needs enforceable code-quality limits before further product growth. A previous
proposal expanded into a policy and admission platform larger than the product. That machinery
is neither necessary nor authorized for this repository gate.

## Decision

Use one local and CI command, `./scripts/quality`. It runs exactly pinned Ruff, Pyright, and
jscpd versions, then a small repository size ratchet. The initial tree is clean for Ruff E9/F/B,
Pyright basic, and qualifying clones at 10 lines and 80 tokens.

New or newly oversized Python files may not exceed 500 physical nonblank lines. A legacy Python
file already above that limit may not grow. Nonignored repository files above 100,000 bytes warn;
above 200,000 bytes fail.

The gate, its configuration, dependency locks, and workflow remain human-review surfaces. This
decision adds evidence for review; it is not a policy engine or an autonomous merge authority.

## Consequences

Agents and humans get the same fast deterministic result locally and in CI. The gate does not
score test usefulness, enforce product architecture, provide waivers, bootstrap its own tools,
or replace semantic review. Those concerns require separate decisions rather than growth here.
