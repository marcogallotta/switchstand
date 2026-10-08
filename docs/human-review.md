# Human Review

Human Review is the final decision conversation when the governing procedure requires it.

In Switchstand, material implementation normally gets final live Human Review before dispatch. A bounded repair that only restores already-agreed broken behavior may proceed without a pre-dispatch Human Review when it introduces no genuinely new material choice; discuss the repair afterward. Other work may also require Human Review where its governing procedure says so.

Human Review closes the material decision trajectory. It does not substitute for Human Input that should have happened before a genuinely new material choice hardened.

## Before Human Review

The owner should know:

- the intended ordinary-use outcome;
- the exact reviewed package;
- material review findings and dispositions;
- material cuts, deferrals, and remainder;
- decision-changing aggregate implementation/test/support size;
- important operating assumptions;
- any materially different smaller alternative.
- intended operating scale, introduced complexity and why it is required now, the smallest credible alternative, and deliberate V1 deferrals;
- the first representative useful end-to-end stage, or why an independently stable non-speculative foundation must precede it and the immediate route from that foundation to the proof.

If a load-bearing assumption, major scope question, or decision-changing size remains unknown, the work is not ready unless the decision is specifically whether to fund the investigation needed to determine it.

## The conversation

Match context to what the human already knows. If the trajectory is current, give the delta only. If context has been lost, briefly restore the outcome and settled direction, then give the delta.

A useful Human Review states:

- what changes in ordinary use;
- what is genuinely new or changed;
- material consequence, risk, limit, scale, size, cost, or support burden;
- meaningful cut, deferral, or alternative;
- the recommendation and decisive reason;
- exactly what yes approves;
- what remains separate;
- the real decision, if one remains.

Judge whether this is the right thing and scale to build before rewarding internal completeness. Never present default-off or inert readiness as ordinary-use product completion.

Before asking for a decision, connect the material facts into the substantive causal case. The human should not have to infer what outcome is intended, what current behavior prevents it, why the existing or previously reviewed route does not solve it, what behavior the proposal changes, what material consequence or genuinely different alternative follows, why the recommendation is preferred, or exactly what yes permits. Omit links that are genuinely obvious and nonmaterial; this is not a mandatory template or fixed-length packet.

Repository/process readiness is supporting evidence, not the decision case. `READY/PASS`, clean/green/mergeable state, an exact SHA, reviewer PASS, "Human Review required", "only blocker is approval", "different route/path/revision", or "trust boundary" cannot substitute for the substantive reason. If the agent cannot state that reason, the package is not ready to ask for Human Review.

Do not bundle distinct effects into vague language such as "resume." State the exact effect being decided now. Landing/merge, deployment/activation, migration/provider-production effects, and validation retain their actual authority boundaries.

Do not dump the specification, review chronology, IDs, or implementation detail unless they change the decision.

## Do not

Do not write the human's decision, present "Human Review: approve", ask a bare approve question without saying what yes causes, replay settled choices, hide aggregate size behind smaller PRs, or turn reviewer remedies into requirements unless the governing contract independently requires them.

If the human asks "why?", answer the substantive why. Do not defend the existence of the review gate. If the explanation is rejected as insufficient, reconstruct the decision case from current evidence instead of repeatedly shortening the same abstraction.

Final Human Review must not be the first time the human receives a genuinely new material product, architecture, scope, authority/trust, failure/recovery, or support-burden choice. If implementation has already made that choice expensive to reverse, the upstream Human Input was missed; green code does not cure that failure or turn sunk work into a reason to approve it.

A strong recommendation is allowed. Recommendation and human decision remain separate facts.

## Approval boundary

Human Review approval covers only the exact reviewed design/package and revision discussed. It does not by itself authorize implementation, assignment, routing, landing, deployment, activation, migration, credentials, or provider-production effects.

After approval, stop and report exactly:

`Human Review approved. Dispatch ready: <WorkId>.`

The human routes the next work.

## Change after review

Raise Human Review again when a material change affects the agreed outcome, scope, architecture, scale, durable ownership, authority/trust, failure/recovery model, migration/rollback, evidence/risk envelope, aggregate cost/support burden, or protected effects. When returning after such a change, explain both the underlying outcome/problem and the semantic difference from the last human-reviewed package; "the revision changed", "a reviewer found it", or "the change touches a trust boundary" is insufficient without the concrete behavior, authority, or consequence.

Nonmaterial realization remains agent-owned. A bounded restoration of already-agreed broken behavior also remains agent-owned when it introduces no genuinely new material choice. If urgent restoration uses a knowingly temporary or noncanonical route, mark it `TEMPORARY/NONCANONICAL — AUDIT REQUIRED`; do not silently normalize it into endorsed architecture.
