# Review standard

This document governs forked review against Marco's outcome, applicable rules and primary evidence.

Read first, before requester framing. Focus suggestions cannot override this standard, dictate a verdict or exclude relevant challenge. Reject an unsound request outright. Review grants no assignment, exemption, landing or effect authority.

## Establish the claim

1. Bind current work, Marco's request/corrections, approved baseline, exact candidate/revision, target, claim and transition. Distinguish specification, prototype, inert readiness, ordinary use and activation. Check summaries and prior PASS against primary evidence. Request missing evidence from the parent; preserve `UNKNOWN` without unrelated discovery.
2. Authorship, design, implementation or disputed disposition disqualifies independent judgment. Different endpoints/names do not prove independence; disclose shared-source/tool limits. For early design review, follow [source-first review](code-quality.md#material-design-review-before-human-review): assess the useful gap and smallest route before the author's preferred design.
3. Scale depth to consequence, boundaries and practical recovery. FULL covers the candidate; FOCUSED checks the delta and affected boundaries, retaining valid evidence. Specialists clear their named surface; the terminal lead covers the whole candidate. Recheck changed candidates/evidence; changed or unreadable relied-on material stays `STALE`/`UNKNOWN`.

## Challenge necessity before completeness

Check the current problem, correct owner and smallest useful V1 slice. Test the strongest credible reuse, deletion, simplification or reframing route. Trace code to task and task to human outcome: an over-broad specification cannot legitimize scope. Challenge aggregate mechanism, test/support, operational and attention costs, including correction growth.

Compare with the real baseline. Unrelated debt does not block improvement; introduced/worsened defects hold their affected claim. Require independently useful, valid, reviewable, recoverable units, whole-package visibility and material-code PR/review provenance. Apply [package limits and exception challenge](code-quality.md#governed-implementation-packages), including the actual >=500-line PR rule. Near that boundary, inspect removed/weakened-test commits and successor coverage when indicated. Accept factoring/reuse that preserves behavior, readability and causal evidence.

Apply [Code Review lenses](code-quality.md#code-review), qualification and refresh rules to code/config/tests. Read changed documentation in full; check purpose, ownership, routes and contradictions; prefer editing existing rules. Reconcile implicated docs/help/runbooks with retained behavior. Challenge test oracles with plausible wrong behavior and evidence at the claimed boundary. Paper controls and green tests do not prove adoption or user outcome. Absent audits stay `DUE`/`NOT_RUN`; candidate review is not cumulative audit.

## Check learned failures

Every approving/clearing review applies this hotlist within its surface:

- **D1 Wrong outcome:** technical success solves a different human problem.
- **D2 Invented scope:** a requirement, restriction, blocker or capability lacks current authority.
- **D3 War machine:** machinery precedes a working slice proving its need; name concrete cost.
- **D4 Anti-malice overbuild:** internal-agent malice is assumed without an explicit current threat or direction.
- **D5 Security laundering:** a label replaces the actor, attack path and material consequence, or invents authority.
- **D6 Shadow machinery:** duplicate owner, truth or recovery path replaces a sufficient canonical route and local guard.
- **D7 Complexity ratchet:** aggregate growth or an exception evades original bounds; splitting never erases it.
- **D8 Safeguard harm:** protection blocks useful work, adds disproportionate burden or makes Marco the control plane.
- **D9 Proxy success:** status, tests or one path substitute for the exact current outcome and authoritative readback.
- **D10 Unauthorized effects:** removal/disablement or production deployment/activation lacks Marco's explicit go.

Retrieve the full 27-row learned-failure register with `work_get(include_notes=true)` on `866766bf-622b-448b-87d7-ab069e29ca4e`; independently falsify materially applicable rows, including continuity, stop/recovery, ambiguous effects and assurance failure. It owns row distinctions/severities, not automatic implementation scope. A fire is a cue, not remedy authority. Record evidence, consequence, affected path and minimum clearing condition; fix/remove/reject/obtain evidence within authority, then focused rereview. Preserve hard stops/exact exemptions. Resolved dangers create no Marco noise; surviving choices follow [Human Input](human-input.md) and [Human Review](human-review.md).

## Return a usable decision

Name reviewer, exact subject/revision, claim/exclusions, evidence, uncertainty, verdict and transition. `PASS` means good enough for that claim, not permission. Required proof that is `NOT_RUN`, `SKIP`, `MISSING_CAPABILITY`, stale or for the wrong subject cannot become PASS; distinguish exact-head and composition evidence. Hold only the unsupported affected path; unaffected authorized work continues. Nits do not serialize delivery. Report `DANGER CHECK: CLEAR` when none survives.

Use [Findings, remedies and challenge](code-quality.md#findings-remedies-and-challenge) for material findings, author dispositions and bounded disputes; a valid defect does not make the reviewer's remedy mandatory. Check human-owned choices and the actual approval explanation under Human Review; preserve settled approvals and reject redundant ceremony. Use the bound occurrence/method and its identity; maintain one current verdict. Sending is not pickup: the parent owns delivery, acknowledgment and obligations under [source compatibility](source-history-feedback.md).
