# ADR 0003: Keep the first code-quality gate small and deterministic

- Status: Accepted
- Date: 2026-08-27

## Context

Switchstand needs enforceable code-quality limits before further product growth. A previous
proposal expanded into a policy and admission platform larger than the product. That machinery
is neither necessary nor authorized for this repository gate.

## Decision

Use one local and CI command, `./scripts/quality`. It runs exactly pinned Ruff, Pyright, and
jscpd versions, then small deterministic text and repository-size checks. The initial tree is
clean for Ruff E9/E501/F/B with a 120-character line length, Pyright basic, and qualifying
clones at 10 lines and 80 tokens.

New or newly oversized Python files may not exceed 500 physical nonblank lines. A legacy Python
file already above that limit may not grow. The companion text check applies the same
120-character physical line limit to human-maintained first-party source, tests, scripts,
documentation, and configuration. Generated fixtures and generated lock data are excluded from
line-length enforcement. Source files have a separate 60 KiB (61,440-byte) ceiling. Every
non-source Switchstand file has a hard 64 KiB (65,536-byte) ceiling.

The gate, its configuration, dependency locks, and workflow remain human-review surfaces. This
decision adds evidence for review; it is not a policy engine or an autonomous merge authority.

## Consequences

Agents and humans get the same fast deterministic result locally and in CI. The gate does not
score test usefulness, enforce product architecture, provide waivers, bootstrap its own tools,
or replace semantic review. Those concerns require separate decisions rather than growth here.
