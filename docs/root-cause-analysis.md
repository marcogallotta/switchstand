# Root-cause analysis

Procedure revision: **RCA-PROCEDURE-V1**

This is the canonical Switchstand process for meaningful root-cause analysis (RCA). It
is shared by ChatGPT and Codex at the semantic level; each host uses its native work,
message, and delegation mechanics. This procedure does not grant authority for an
effect, create a project, or prove that a correction has been installed or validated.

## Bounded triggers

Open or reuse one RCA when Marco requests it, a credible live-user incident occurs, a
material control or ambiguous-effect boundary is escaped, a meaningful failure recurs,
or a high-impact near miss exposes the same causal need. Routine one-off friction,
hypothetical brainstorming, and a minor known error with an obvious local fix do not
need a full RCA unless current routing or Marco explicitly requires one. Record those
through their existing owner instead.

A live incident enters the [incident runbook](operations-live-incident.md) first. RCA
work never delays acknowledgement, current-state inspection, or the smallest safe
mitigation. Resolve or create the RCA identity as soon as it is safe.

## Identity, authority, and evidence

Use one sanitized RCA parent WorkId in the dedicated RCA project selected by current
`START HERE` routing. Reuse it for the same causal investigation; do not create parallel
RCA records for technical and response-process analysis. Record the procedure revision
used, but compare it with the current procedure on re-entry rather than freezing stale
instructions. Project membership, the procedure, and readable evidence route work; they
do not grant an effect.

Keep the parent record sanitized and useful for discovery. It contains the affected
outcome, current state, incident or work identity, bounded impact, owner, hypotheses,
public-safe evidence summaries and private evidence references, checkpoints, child
correction WorkIds, and validation state. Raw logs, secrets, credentials, personal data,
and other sensitive evidence stay in the private incident/evidence location with only a
bounded reference on the parent.

Every material RCA has one parent owner. That owner maintains the causal model, starts
only proportional non-interfering evidence lanes, reconciles their results, requests
independent causal-model review, and actively sweeps corrections through validation.
Evidence lanes are read-only unless separately authorized, have one question and one
owner, and may not compete with incident mitigation or overlap a writable surface or
semantic concern.

## Immediate acknowledgement and checkpoints

Immediately acknowledge the request without pretending to know the answer. Use one of:

- `PRELIMINARY — <specific hypothesis>; confidence <low|medium|high>; evidence <supporting
  and contradicting evidence>; alternatives <credible alternatives>; next falsifier <check>.`
- `UNKNOWN — no supported causal hypothesis yet; checking <first discriminating evidence>.`

An acknowledgement is not a conclusion. While Marco is actively engaged, publish short
visible checkpoints after each material evidence result and before a nontrivial tool or
wait interval. Each checkpoint states current impact/status, leading hypothesis and
confidence, evidence gained or falsified, current action, next checkpoint, and any Human
or agent action. Never imply that an inactive ChatGPT chat or stopped Codex session will
continue checking in the background.

## Investigation method

Start with current evidence and label mutable claims `CURRENT`, `HISTORICAL`, or
`UNKNOWN`. Preserve exact time, candidate/runtime/configuration, operation identity, and
evidence path. Separate observation from inference and effect attempt from readback.

Confidence applies only to a specific causal hypothesis. It is a compact
`LOW`/`MEDIUM`/`HIGH` judgment of how the current evidence discriminates that hypothesis
from credible alternatives, not a probability attached to a fact or effect. Every confidence
report names its supporting and contradicting evidence, credible alternatives, and next
falsifier or discriminating check. When there is not enough evidence for that structure, report
`UNKNOWN` and the first clearing check instead of inventing confidence.

Observed facts, currentness, gate results, and effect truth retain their deterministic labels,
readbacks, and existing failure states. Raw self-confidence cannot establish observed fact or
effect truth, override `UNKNOWN`, authorize an effect, move an RCA or incident lifecycle state,
accept a causal model, establish recovery, satisfy completion, or close work. Those transitions
continue to require their existing evidence, authority, review, and closeout gates. If a separate
situation-review safeguard applies, evidence-backed confidence may be one advisory input; low
confidence alone is not a trigger, and this procedure creates no duplicate review trigger.

Maintain a compact hypothesis table on the RCA parent:

| Hypothesis | Mechanism | Supporting / contradicting evidence | Credible alternatives | Falsifier / discriminating check | Confidence | Result |
| --- | --- | --- | --- | --- | --- | --- |
| ... | ... | ... | ... | ... | LOW/MEDIUM/HIGH | OPEN/SUPPORTED/FALSIFIED/UNKNOWN |

Test the cheapest safe discriminating boundary first. Credible alternatives stay in the
table until evidence falsifies them; popularity or narrative fit is not evidence. If the
investigation needs a consequential product, architecture, risk, trust, or scope choice,
surface Human Input before hardening it.

Before inventing a rule, tool, or product, audit:

1. prior RCAs and recurrences for the same mechanism or control boundary;
2. whether their corrections were installed, activated where required, exercised by a
   matching event, and actually effective;
3. current owning-product requirements, tests, monitoring, and infrastructure;
4. current routed product work, which owns current status and priority, and the
   historical roadmap as evidence only for an existing owner or planned capability; and
5. activation/reliance state, because inert landing is not installed behavior.

The audit can conclude that an existing correction is missing, inactive, ineffective,
or bypassed. Fix that causal gap before proposing duplicate machinery.

## Terminal causal standard

A terminal causal model names the trigger, failure mechanism, contributing conditions,
failed or absent controls, detection and response behavior, credible alternatives and
falsifiers, and the counterfactual control that would have prevented or bounded the
event. Timeline, apology, blame, or “agent did not follow instructions” alone is not RCA.

State confidence for each material causal claim and cite its evidence. Explain why the
supported mechanism fits the observations better than remaining alternatives. If a
required claim remains `UNKNOWN`, preserve it and its clearing check; do not promote a
preliminary story to a terminal model.

The exact causal model receives independent review before `CAUSAL_MODEL_ACCEPTED`.
Review applies the finding standard in [code quality](code-quality.md): defect, evidence,
consequence, affected claim, and minimum clearing condition. Review is evidence, not
authority or a demand to adopt the reviewer's preferred remedy.

## Corrections and product ownership

Classify every accepted correction; do not hide product work inside process prose:

| Class | Meaning | Required disposition |
| --- | --- | --- |
| Immediate stopgap | Reversible containment that bounds current harm | Exact owner, installed state, removal trigger, and proof |
| Existing product requirement/test | The owning product already promises the behavior | Child WorkId on that product; repair the owner and causal regression |
| Genuinely new product | No current product owns the needed capability | Child WorkId routed through product planning and Human Input before hardening |
| Process/documentation | Shared behavior or discoverability is the causal owner | Child WorkId for the canonical procedure or entry point, with focused reachability/content evidence |

Each material correction gets its own child WorkId routed to the owning product or
canonical process surface and linked from the RCA parent. The parent remains the causal
and validation index; it is not a substitute owner for implementation, activation, or
provider effects. A correction's authority, review, landing, activation, and reliance
gates remain those of its owning product.

## Lifecycle and active follow-up

Track the RCA parent through:

`INVESTIGATING -> REVIEW_READY -> CAUSAL_MODEL_ACCEPTED -> CORRECTIONS_OWNED -> VALIDATING -> CLOSED`

These states are separate from incident recovery. `MITIGATED` or incident `CLOSED` does
not mean the RCA or its corrections are validated. Conversely, a causal model can be
accepted while long-term correction work remains open. Incident closeout follows its
own non-deferrable recovery gate and requires the accepted causal work plus explicit
ownership of every residual correction.

The RCA owner performs an active follow-up sweep at startup, on re-entry, at every
bounded work-batch boundary, before handoff, and when a correction due or unblock event
occurs. For every correction, preserve:

| Child WorkId / class | Owner | Next action | Dependency / return trigger | Next check | Required proof | Stopgap removal | State |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ... | ... | ... | ... | ... | ... | ... | OPEN/BLOCKED/VALIDATING/VALIDATED/RETIRED |

Writing the RCA, assigning a child, sending a message, or receiving review does not close
the work. The owner continues the exact watches while its session can run and hands off
only through explicit attributable pickup. At a real host limit, persist the exact owner,
state, next action, dependency, next check, proof, and terminal condition, then state that
active checking stopped. Durable state supports re-entry; it is not an inactive wake or
background daemon.

No correction may gather dust as an unowned note, undated backlog item, or link without a
next check. If work is deliberately deferred or retired, record Marco's exact decision,
the consequence, and the event that would reopen it. A stopgap remains open until its
replacement is validated and removal is proved.

## Validation and closure

Validate a material correction with a safe controlled replay of the original trigger and
control boundary. When replay would be unsafe, destructive, or impractical, use the next
eligible natural matching event and keep the correction `VALIDATING` with exact owner,
instrumentation, proof, and return trigger. Synthetic, unit, or documentation evidence
may prove a narrower landing claim but cannot establish live reliance that depends on an
external or client boundary.

Close the RCA only when:

- the independently reviewed causal model is accepted;
- every correction is validated, or explicitly retired/deferred by Marco with consequence
  and return trigger;
- each temporary stopgap is removed or deliberately adopted with its own proof;
- the matching replay/event evidence and remaining `NOT_RUN`, `MISSING_CAPABILITY`, or
  `UNKNOWN` claims are stated exactly; and
- the final parent record names the outcome and links all retained private evidence and
  product-owned work.

Documentation reachability tests prove only that this procedure and its entry links are
present. They do not prove agent adoption, investigation quality, correction installation,
activation, or validation in a matching event.

## Parent record template

```markdown
# RCA — <affected outcome>

- Procedure revision: RCA-PROCEDURE-V1
- RCA WorkId / project route: <exact identity / current dedicated RCA project>
- State: INVESTIGATING | REVIEW_READY | CAUSAL_MODEL_ACCEPTED | CORRECTIONS_OWNED | VALIDATING | CLOSED
- Parent owner / next check: <owner / exact event or time>
- Incident or triggering work: <exact identity>
- Impact and boundaries: <sanitized facts>
- Acknowledgement: PRELIMINARY <hypothesis/confidence/falsifier> | UNKNOWN <first check>
- Private evidence: <bounded paths or identities; no raw secrets>
- Prior RCA / product / roadmap / activation audit: <result and links>
- Causal model / confidence / alternatives / counterfactual: <summary and evidence>
- Independent review: <exact candidate/model, reviewer, verdict, open findings>
- Correction sweep: <child WorkIds with owner, next action, dependency, check, proof, stopgap removal>
- Residual truth: <NOT_RUN/MISSING_CAPABILITY/UNKNOWN/Human Input or none>
- Final matching replay/event proof: <identity and result>
```
