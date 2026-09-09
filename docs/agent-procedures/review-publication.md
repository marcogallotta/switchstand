# Review and publication

## ENTRY CONDITIONS
Use this procedure when a candidate is ready for tests/quality evidence, fresh independent review, rereview, PR/publication preparation, or another governed promotion boundary.

## REQUIRED INPUTS / ROUTES
- Exact candidate identity/diff or other immutable revision identity.
- Controlling work/design/Contract and acceptance evidence required by it.
- Explicit publication/effect authority; candidate preparation never implies commit/push/PR/merge/release authority.

## ALLOWED WRITES / EFFECTS
Read-only review and tests within the assigned candidate surface. Publication effects only when the active work explicitly authorizes the exact effect. Review findings do not authorize applying or publishing fixes on another surface.

## REQUIRED STEPS
1. Run affected tests and the full required quality gate before publication unless the controlling work specifies a different exact gate. Review materially changed AI-authored tests for the fault they actually catch.
2. Bind evidence to the exact candidate/repository identity; do not reuse stale or wrong-head evidence as current proof.
3. Before publication, obtain one fresh bounded independent read-only review of the exact candidate. Add another reviewer only for a materially independent surface where it adds information.
4. Fix blocking findings only on an authorized writable surface. Use the same reviewer for one targeted rereview of those fixes; do not recursively review rereview-only changes.
5. Preserve minor/non-blocking findings with disposition instead of allowing review to expand scope.
6. Recheck explicit authority immediately before commit/push/PR/merge/release or other protected effect and perform authoritative readback afterward.
7. For meaning-changing Project-settings/AGENTS/procedure changes, review the complete literal revision, retrieval routes, semantic moves/deletions, behavior canaries, release order and rollback before Marco approval.

## STOP CONDITIONS
Stop on failed required quality evidence, unresolved blocking review finding, stale/wrong candidate identity, missing protected-effect authority, material scope/design expansion from review, or ambiguous publication effect.

## DURABLE WRITEBACK
Record exact candidate identity, tests/evidence, reviewer/verdict, finding disposition, remaining unknowns and the exact next authorized effect on the owning work/checkpoint.

## EXIT / NEXT PROCEDURE
If review exposes a true material decision or blocker, use `learning-escalation.md`. Otherwise return to the active work for its explicitly authorized publication/promotion step.