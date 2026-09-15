# Code quality and Code Review

This is the repository semantic owner for substantive code/config/test implementation and Code Review. It complements current active work, approved Design/Implementation Contract, worker-owned Execution Plan, and routed review procedure; it never grants scope, effect, merge, activation, or approval authority.

## Governing standard

A good change delivers the exact governing behavior with the simplest sound current design, lives in the correct owner, preserves explicit authority/trust/resource/dependency boundaries, avoids duplicate truth and speculative machinery, has discriminating maintainable evidence, and leaves the touched system no harder for a fresh competent agent to understand and safely change. Green tests are evidence, not the definition of correctness or quality.

## Author / implementer

Before material coding:
1. Bind the exact governing outcome, non-goals, current Design/Contract/Plan, candidate/base and writable surfaces.
2. For nontrivial work, make the existing worker-owned Execution Plan concrete enough for a fresh worker to identify: the canonical owner/reuse/delete route; intended changed surfaces and ownership boundaries; the strongest materially smaller credible alternative when the choice is architecture-sensitive; the earliest real/discriminating test or proof; the expected implementation/test-support envelope; and material unknowns. The Execution Plan must also state the proposed PR/layer decomposition and why the chosen cut beats the obvious smaller alternative. The justification is semantic: independently useful/valid behavior, meaningful acceptance evidence, and independent rework/rollback. Line count is not the justification. This is not a second quality plan or a new approval artifact.
3. State a tiny behavioral contract where the claim is material: governing preconditions/inputs, required output/postcondition, relevant failure/UNKNOWN/undefined boundary, and the earliest discriminating example or boundary proof. This is not universal formal-spec or TDD ceremony.
4. Choose the earliest practical evidence capable of falsifying the real claim. Where implementation and tests/mocks can share the same false model — provider, Git, persistence, subprocess, runtime or other external semantics — include proportionate real/hermetic boundary evidence rather than letting the mock world define truth.
5. Treat the expected implementation envelope as an alarm, never as a budget to spend. Large/unexpected mechanism, owner/trust surface, defensive machinery, fixture/support burden or aggregate stack growth is a stop-and-classify signal. Current routed procedure controls any exact hard size/exemption rule.

During implementation:
- Fix the causal owner; do not patch callers/tests around the defect.
- At each quality refresh, compare the actual trajectory with the current Execution Plan. Unexpected new owner/provider/persistence/trust/interface/external-I/O/recovery surface, materially larger defensive/test-support machinery, or loss of the planned discriminating proof means stop/replan/classify the affected path before adding another patch layer. Routine local/private/reversible mechanics remain worker-owned.
- Keep one source of truth and explicit boundaries. Avoid hidden global side effects, duplicate policy/state/config/currentness, and new local convenience authorities.
- Preserve legibility: current behavior ownership, failure/UNKNOWN semantics, executable contracts and relevant docs must remain discoverable.
- Tests target governing behavior/invariants and plausible faults, not private call sequencing. Do not change the oracle to make the implementation pass.
- Preserve demonstrated causal regression cases when recutting a bad mechanism; do not preserve accidental scaffolding merely because a prior test encoded it.
- Test-support code is part of the maintenance surface. A large mock/fixture world that reproduces implementation choreography or invents provider/Git/runtime semantics is a quality-defect signal even when production code is small.
- If a valid sequence of local fixes materially changes the whole mechanism, ownership/trust surfaces, implementation envelope, or test/support burden, stop local ratcheting and reset: should this solution still exist in this form, and is there now a smaller reuse/delete/reframe route?
- Existing repository debt never authorizes new local debt.

## Test quality and qualification

Prefer the smallest maintainable test set that falsifies the governing behavior and plausible faults. Each material test should have a causal reason to exist; where practical, identify a plausible wrong implementation/fault that the material oracle would reject. Test quantity, assertion count, coverage, or mock realism is not a quality strategy by itself.

Use the highest practical fidelity needed by the claim. Unit/fake/mock evidence is appropriate for local semantics it can truthfully model. Material provider/Git/persistence/subprocess/runtime/external claims require proportionate real or hermetic-boundary evidence when the lower-fidelity test can share a false model. Removing redundant implementation-detail tests is not assurance loss when the causal oracle and required boundary evidence remain.

Qualification is claim-specific and proportionate to consequence, blast radius and practical rollback. Use the cheapest safe evidence that actually exercises the claimed boundary. Do not default every change to a live/external run, and do not let an isolated/mock run establish a claim that depends on live provider/runtime behavior.

Keep two claims separate when applicable:
- **LANDING / INERT PROOF:** whether the exact candidate/composition can safely land in its reviewed default-off/inert configuration under the current contract.
- **ACTIVATION / RELIANCE PROOF:** whether enabling or relying on the live external/runtime/user path works safely under the activation contract.

A default-off/inert change may be landing-qualified without live activation when activation is not part of the landing claim. Conversely, landing green never proves activation/reliance. Never substitute activation-only evidence for an unresolved landing consequence, or landing-only evidence for a live activation claim.

Named required gate + missing capability, NOT_RUN, SKIP, ambiguous readback or wrong subject identity means that exact claim is not established; it is not candidate failure unless the candidate actually failed, and it is not PASS. Preserve exact-head evidence separately from synthetic/base-composition evidence.

## Code Review

For a substantive candidate, review the exact governing behavior and exact immutable candidate/head. Review depth follows semantic risk, blast radius and practical rollback, not cosmetic size.

Ask in order:
A. **Should this solution exist?** Correct problem and owner? Root fix? Smaller reuse/delete/reframe route?
B. **Whole-system effect.** Does local success create duplicate truth, dependency/authority drift, hidden behavior, residue/coupling, disproportionate operational/support burden, or worse agent navigation?
C. **Implementation quality.** Correctness, failure/UNKNOWN semantics, applicable concurrency/idempotency/external semantics, cohesion/readability, unnecessary cleverness; does the implementation still match the concrete owner/reuse/evidence shape of its Execution Plan?
D. **Tests as adversarial artifacts.** What concrete fault does each material test catch? Would plausible broken code fail? Are mocks/fixtures fabricating the real boundary? Is important causal regression or high-fidelity boundary evidence missing? Is the test-support architecture itself becoming a maintenance liability?
E. **Outcome / qualification.** Does the candidate deliver the governing behavior independently of its own tests? Is each landing/activation claim supported by the right evidence subject? Preserve NOT_RUN/SKIP/MISSING_CAPABILITY and exact-head-vs-composition truth.
F. **Scope / decomposition.** Does each PR/layer represent the smallest independently useful, valid, reviewable and recoverable intermediate state? Could an obvious smaller slice safely land with meaningful acceptance evidence and independent rollback/rework? If yes, prefer it. If no, name the reason the behavior/evidence/recovery must remain together. Dependency order alone does not justify bundling; sub-500 splitting does not prove good decomposition; split laundering is a defect.

PASS means materially good enough for the exact reviewed claim, not perfect and not approval/landing/activation authority. Style/nits do not serialize work.

## Findings, remedies and challenge

A material review finding states:
- exact defect/invariant;
- evidence;
- consequence/materiality;
- affected claim/path;
- minimum clearing condition.

A valid finding does not make the reviewer's proposed implementation shape authoritative. A proposed remedy is advisory unless the controlling Design/Contract already requires it. The author must disposition a material finding as one of:
- **ACCEPT** — defect is valid; author chooses the smallest authorized clearing fix;
- **ACCEPT_DEFECT_REJECT_REMEDY** — defect is valid, proposed remedy is over-scoped; author supplies a smaller clearing route;
- **CHALLENGE** — applicability/materiality/causality/evidence is disputed with exact counterevidence;
- **NEEDS_EVIDENCE** — the claim cannot yet be established safely.

A real blocker continues to block only its affected consequential claim/path until cleared or adjudicated. Unaffected authorized work continues.

ACCEPT_DEFECT_REJECT_REMEDY accepts the defect but does not clear it. The author implements/proposes the smaller authorized clearing route and returns the exact corrected candidate/evidence to the original reviewer for focused rereview against the original defect and minimum clearing condition. The reviewer then WITHDRAWS/NARROWS/UPHOLDS/returns NEEDS_EVIDENCE on that exact claim. If a material applicability/materiality/clearing dispute still survives after that bounded focused response, Coordinator acquires one fresh independent reviewer for exact-dispute adjudication only. Until cleared/adjudicated, the finding remains open and holds only the affected consequential claim/path.

Author/reviewer agreement does not defeat the correction-ratchet reset. If cumulative accepted corrections materially change mechanism, ownership/trust surfaces, implementation envelope, or test/support burden, re-run the whole-current-candidate `should this solution exist? / smaller reuse-delete-reframe?` judgment before another local patch layer.

## Refresh check

When the current bootstrap/routed procedure calls for a quality refresh, reopen this exact candidate/control-SHA version and re-ground only these questions:
1. What exact outcome/non-goals and current canonical owner/reuse seam/Execution Plan shape govern the next work?
2. Has the solution shape materially changed, and is there now a smaller delete/reuse/reframe route?
3. What claim-specific oracle/evidence is still missing, NOT_RUN, SKIPPED, MISSING_CAPABILITY, or UNKNOWN?
4. Is the next material commitment still inside the approved scope/authority/boundary and supported by the current plan/evidence?

This refresh is a reasoning re-ground, not a new approval gate, checklist, timer, or authority source.

## Deterministic and execution truth

The repository's current deterministic quality gates remain whatever the current executable contract/CI actually requires. Never convert NOT_RUN, SKIP, missing capability, wrong-candidate execution, or synthetic-merge/composition evidence into an exact-head PASS claim. Structural/complexity evidence is diagnostic unless an exact current mechanically enforced invariant says otherwise.

This guidance can require plan shape, refresh/replan behavior, test evidence and later review checks; it does not prove the agent actually complied during the coding trajectory. This package contains no deterministic trajectory monitor/evidence gate. Treat conformance during implementation as guidance + readiness/review-backed detection until natural adoption evidence demonstrates behavior. Future mechanical enforcement remains a separately reviewed design problem.

Broader cumulative code-health/audit scheduling belongs to the existing audit owner/process, not this document.
