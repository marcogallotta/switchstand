# Work entry

## ENTRY CONDITIONS
Use this procedure when starting/restarting Switchstand work, resolving the active WorkId and bounded references, verifying the repository base, or using the temporary exact-task handoff bridge.

## REQUIRED INPUTS / ROUTES
- Active work or Marco’s exact assignment.
- Bounded references advertised by the active work.
- Exact repository base required by the work.
- Temporary bridge owner `1218278195229493` only when the handoff explicitly activates it.

## ALLOWED WRITES / EFFECTS
Reading work/references and setup required by the active assignment. No provider discovery, claim, assignment, mutation, Git publication, credential expansion, or cross-task write follows from entry/setup alone.

## REQUIRED STEPS
1. Call `work_get` without a WorkId; read the active work and its bounded references.
2. Verify the exact green repository SHA required by the active work before material edits.
3. Use `~/.config/switchstand/.env` for setup. Ask once for a genuinely missing required value, write it there when the active setup path authorizes that write, and reuse it. Asana REST uses `ASANA_TOKEN`; any OAuth layer is GitHub-only, never Asana OAuth.
4. Default to Switchstand for work discovery.
5. Temporary exception: only when the active handoff explicitly activates the Asana-handoff exception and names exact Asana task ID(s), Codex may use the local `asana` CLI read-only for only those exact tasks/references. No search/list/project sweep, discovery, claim, assignment, mutation, or broader provider access. Missing/inconvenient WorkId does not activate it. The exception ends once work is Switchstand-bound.
6. Treat `marcogallotta/switchstandold` as read-only evidence, never an implementation base.
7. Load the procedure(s) required for the next material action.

## STOP CONDITIONS
Stop before material action when active work/authority cannot be established, required references are ambiguous, the verified base is not the required green base, or proceeding would require ungranted provider/credential/effect authority.

## DURABLE WRITEBACK
Record a material entry/recovery blocker on the assigned writable work/checkpoint with the verified fact, unknown, and next action.

## EXIT / NEXT PROCEDURE
Proceed to `execution.md`, `review-publication.md`, or `learning-escalation.md` according to the active action/route.