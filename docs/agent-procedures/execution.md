# Execution

## ENTRY CONDITIONS
Use this procedure for authorized research, design/candidate preparation, implementation, editing, bounded child work, or other material execution on the active work.

## REQUIRED INPUTS / ROUTES
- Active work, governing references, exact writable surface, current repository/base identity where relevant.
- Any controlling Design/Contract or explicit stop boundary named by the work.

## ALLOWED WRITES / EFFECTS
Only the active WorkId / explicitly assigned writable result surface and explicitly delegated repository/files/effects. Parent, reviewed and reference targets remain read-only unless separately authorized.

## REQUIRED STEPS
1. Read the active work and governing references before material edits or child dispatch.
2. Keep one agent owner per writable surface. Children receive bounded objective, files/surface, required evidence/tests, stop conditions and result destination.
3. Inspect a child after roughly a minute or when behavior is suspicious; classify it as in-scope, drift, or stop. Steer or stop drift rather than accepting new scope.
4. Keep changes inside assigned stage/objective and file ownership. Shared config, migrations, CI and shared guidance belong to the integration/semantic owner unless explicitly delegated.
5. Treat a stage as a coordination/result owner, not automatically a PR boundary. Before editing where publication is expected, decompose into the smallest independently coherent/verifiable change units. Each unit has one primary behavioral outcome and reason to change; dependency/timing alone does not justify bundling. Large diff/file spread is a split signal, not a numeric law.
6. Reread before replacement writes; preserve stale-state failures; read back successful effects. Never blind-retry an ambiguous external effect.
7. Keep the durable owner/checkpoint current after material direction/state change, around dispatch, when a child terminates, and before handoff/final/context replacement.

## STOP CONDITIONS
Stop the affected path on new material authority, persistence, concurrency, security, external-interface, recovery-semantics or hard-to-reverse choice; changed controlling baseline; unauthorized cross-surface write; material scope expansion; or failed authoritative readback.

## DURABLE WRITEBACK
Write bounded current state, decisions, evidence pointers, unknowns/blockers, live-child terminal results and next action to the assigned owner/checkpoint. Temporary child results are absorbed once by the owner and the child is completed.

## EXIT / NEXT PROCEDURE
Use `review-publication.md` before publication/review gates. Use `learning-escalation.md` for surviving blockers, friction, hard stops or escalation.