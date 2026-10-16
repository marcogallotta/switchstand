# Human Review

Human Review is the final decision conversation when the governing procedure requires it.

In Switchstand, every material implementation gets final live Human Review before dispatch. Other work may also require Human Review where its governing procedure says so.

Human Review closes the material decision trajectory. Prior Human Input is expected where a consequential choice actually required it, but is not a prerequisite.

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

Do not dump the specification, review chronology, IDs, or implementation detail unless they change the decision.

Assume the human may read only this opening, in this order:

```text
WHAT YOU ASKED — <intended or approved outcome in the human's framing>
WHAT THIS DOES — <actual ordinary-use change>
WHAT THIS DOES NOT DO / WHAT CHANGES FOR YOU — <exclusions, cuts, burden, limits, or consequences>
SIZE — <decision-useful implementation/support size or operating burden, with uncertainty>
```

Add `RECOMMENDATION` and `DECISION` only when needed. No decision-changing fact may appear only
after the opening. When Human Review is required, the author includes this actual proposed opening
in the existing design/implementation/deviation review; findings that change its decision surface
receive focused rereview, not a new review-of-review stage.

## Do not

Do not write the human's decision, present "Human Review: approve", ask a bare approve question without saying what yes causes, replay settled choices, hide aggregate size behind smaller PRs, or turn reviewer remedies into requirements unless the governing contract independently requires them.

A strong recommendation is allowed. Recommendation and human decision remain separate facts.

## Approval boundary

Human Review approval creates or supersedes an immutable approved baseline for the exact reviewed
package. It preserves the attributable human sources, approved meaning, protected requirements,
non-goals/deferrals, decision-relevant size/burden, scale and risk boundaries, exact occurrence,
package revision, decision, and named continuation effects. History is append-only.

Approval clears Human Review for the continuation named in that exact handoff. Continue through
implementation, landing, or activation only when the current direct assignment or `CURRENT` grant
separately authorizes that effect and surface. Approval does not itself authorize assignment,
routing, landing, deployment, activation, migration, credentials, or provider-production effects.
When continuation authority or pickup is separate, report that exact next handoff; approval alone
does not require a stop or another return to the human.

## Baseline preservation and deviation

Before implementation dispatch, the owner records `BASELINE_PRESERVED`, `MATERIAL_DEVIATION`, or
`UNKNOWN` against the exact approved baseline and implementation package. Existing independent
design/implementation review validates that comparison. Only an exact reviewed
`BASELINE_PRESERVED` package may dispatch; `UNKNOWN` obtains minimum evidence and blocks only that
dispatch claim while correction, investigation, unaffected branches, and safe option-preserving
work continue.

Removal or weakening of a protected requirement is material: deletion/de-scope, deferral, making
required behavior optional, materially narrowing cases, adding a weakening exception/manual hoop,
weakening recovery/operability, or relabeling incomplete work as complete. It must be resolved
before implementation of the affected branch. The reviewer tests a credible smaller
baseline-preserving route before routing a surviving mature human-owned tradeoff to focused Human
Review. Review churn is a signal, never a numeric trigger.

Final independent implementation/code review binds the exact candidate to the approved baseline.
`CONFORMS_TO_APPROVED_BASELINE` requires preserved protected requirements and non-goals, no
unresolved material deviation, and no new/materially changed human-owned outcome, product
experience, architecture choice, trust/data boundary, operating/support/cost burden, material risk,
or protected effect. A conforming build continues through otherwise-authorized gates without a
second Human Review solely because implementation is complete. `MATERIAL_DEVIATION` or
`NEW_HUMAN_CHOICE` returns only the affected choice before commitment; `UNKNOWN` establishes no
conformance claim and does not become a portfolio hold.

## Change after review

Raise Human Review again when a material change affects the agreed outcome, scope, architecture, scale, durable ownership, authority/trust, failure/recovery model, migration/rollback, evidence/risk envelope, aggregate cost/support burden, or protected effects. Nonmaterial realization that conforms to the approved baseline remains agent-owned.
