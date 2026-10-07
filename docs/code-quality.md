# Code quality and Code Review

This is the repository semantic owner for substantive code/config/test implementation and Code Review. It complements current active work, approved Design/Implementation Contract, worker-owned Execution Plan, and routed review procedure; it never grants scope, effect, merge, activation, or approval authority.

## Governing standard

A good change delivers the exact governing behavior with the simplest sound current design, lives in the correct owner, preserves explicit authority/trust/resource/dependency boundaries, avoids duplicate truth and speculative machinery, has discriminating maintainable evidence, and leaves the touched system no harder for a fresh competent agent to understand and safely change. Green tests are evidence, not the definition of correctness or quality.

## Author / implementer

Prefer the smallest safe vertical end-to-end stage that produces representative useful behavior and evidence. Use a foundation-only or inert first stage only when a vertical stage is unsafe or impractical; state why, keep the foundation independently stable, exclude speculative later-stage machinery, and name the immediate route to the first representative useful end-to-end proof. Default-off or inert readiness is a landing claim, not ordinary-use product completion.

Before material coding:
1. Bind the exact governing outcome, non-goals, current Design/Contract/Plan, candidate/base and writable surfaces.
2. For nontrivial work, make the existing worker-owned Execution Plan concrete enough for a fresh worker to identify: the canonical owner/reuse/delete route; intended changed surfaces and ownership boundaries; the strongest materially smaller credible alternative when the choice is architecture-sensitive; the earliest real/discriminating test or proof; the expected implementation/test-support envelope; and material unknowns. The Execution Plan must also state the proposed PR/layer decomposition and why the chosen cut beats the obvious smaller alternative. The justification is semantic: independently useful/valid behavior, meaningful acceptance evidence, and independent rework/rollback. Line count is not the justification. This is not a second quality plan or a new approval artifact.
3. State a tiny behavioral contract where the claim is material: governing preconditions/inputs, required output/postcondition, relevant failure/UNKNOWN/undefined boundary, and the earliest discriminating example or boundary proof. This is not universal formal-spec or TDD ceremony.
4. Choose the earliest practical evidence capable of falsifying the real claim. Where implementation and tests/mocks can share the same false model — provider, Git, persistence, subprocess, runtime or other external semantics — include proportionate real/hermetic boundary evidence rather than letting the mock world define truth.
5. Treat the expected implementation envelope as an alarm, never as a budget to spend. Large/unexpected mechanism, owner/trust surface, defensive machinery, fixture/support burden or aggregate stack growth is a stop-and-classify signal. Current routed procedure controls any exact hard size/exemption rule.

During implementation:
- Fix the causal owner; do not patch callers/tests around the defect.
- At each quality refresh, compare the actual trajectory with the current Execution Plan. Unexpected new owner/provider/persistence/trust/interface/external-I/O/recovery surface, materially larger defensive/test-support machinery, or loss of the planned discriminating proof means stop/replan/classify the affected path before adding another patch layer. Routine local/private/reversible mechanics remain worker-owned.
- When actual work materially outgrows or invalidates the planned PR/layer shape, the implementation parent/integration owner — or the sole implementer when there is no parent — must make an explicit **KEEP ONE PR / SPLIT / REPLAN** decision before the implementation expands further. **KEEP ONE PR** when the current behavior, evidence and recovery form one coherent independently reviewable unit and splitting would create a misleading or invalid partial state. **SPLIT** when a smaller independently useful, valid, reviewable and recoverable slice now exists with meaningful acceptance evidence and independent rollback/rework; identify that first slice and why it is better than continuing as one PR. **REPLAN** when the mechanism, owner/trust boundary, required evidence, or recovery shape has materially changed. Do not merely raise a size alarm, defer the decomposition decision to later Code Review, or split to satisfy a numeric threshold; escalate only when the resulting decision crosses an existing material authority/review/exemption boundary.
- Keep one source of truth and explicit boundaries. Avoid hidden global side effects, duplicate policy/state/config/currentness, and new local convenience authorities.
- Preserve legibility: current behavior ownership, failure/UNKNOWN semantics, executable contracts and relevant docs must remain discoverable.
- Tests target governing behavior/invariants and plausible faults, not private call sequencing. Do not change the oracle to make the implementation pass.
- Preserve demonstrated causal regression cases when recutting a bad mechanism; do not preserve accidental scaffolding merely because a prior test encoded it.
- Test-support code is part of the maintenance surface. A large mock/fixture world that reproduces implementation choreography or invents provider/Git/runtime semantics is a quality-defect signal even when production code is small.
- If a valid sequence of local fixes materially changes the whole mechanism, ownership/trust surfaces, implementation envelope, or test/support burden, stop local ratcheting and reset: should this solution still exist in this form, and is there now a smaller reuse/delete/reframe route?
- Existing repository debt never authorizes new local debt.

## Governed implementation packages

For a governed implementation task, keep one compact package projection in the existing
task/Execution Plan. It carries the approved record; it is not a second approval artifact,
package database, or parallel review ceremony. At approval freeze:
- package identity, exact approved record revision and immutable package base SHA;
- included package surfaces and which of them are support (tests/fixtures/tools/docs);
- the ORIGINAL APPROVED upper forecasts for production and support;
- the explicit approved margins and resulting production, support and total hard caps;
- the strongest credible simpler alternative, economy proof and material replan triggers.

### Mandatory scope blocks

Before Human Review, every material Design Specification records the intended repository
surfaces/patterns; original production/config and support forecasts; production, support and
total hard caps; the V1 binary allowance fixed at zero changed files and zero changed bytes;
counts of new tables, tools, services, processes, flags, grant operations, persistent stores or
state machines, and runtime-config crossings; the strongest materially smaller route; and the
exact breach/replan condition. Missing or unresolved fields mean **NOT READY**.

Before dispatch, its material Implementation Specification reconciles those fields, freezes one
immutable package base SHA, declares package patterns covering every base-to-final-head changed
path, uses support patterns only to classify covered paths, copies the original forecasts/caps,
records the exact `package-size.py` command, and names one cumulative package/integration owner.
Missing fields or unresolved `UNKNOWN` mean **NOT READY**. Revised estimates, rebases and PR
slicing never reset the base, forecasts or caps.

The report requires the base to be an ancestor of the combined head and measures their one Git
diff. Any uncovered changed path returns `UNKNOWN` without a cap comparison. Any added, deleted
or content-changed binary also returns `UNKNOWN`; only a Git-detected pure rename whose old and
new blob OIDs are identical is reported at zero gross and allowed by V1. Configurable nonzero
binary allowances are deferred.

Repository gross LOC and external activation effects remain separate. A process-note or receipt
update is outside the Git report and cannot be estimated by a second counter or used to reset its
cap. When a governing package includes such effects, bind them to exact existing WorkIds and
fields, preserve unrelated text, use exact CAS/operation receipts, reread exact resulting text
and revisions, and require replan for any broader durable surface or policy effect. Repository
landing alone never proves that process activation occurred.

Forecasts are alarms, not targets. Later re-estimates may guide execution but never reset the
original-forecast denominator or raise an approved cap. Do not maintain a running line-count
ledger. Recompute retained fixed-base counts from Git with `python scripts/package-size.py`
using the governing record's base, exact combined head, package patterns, support patterns,
original forecasts and approved caps. The script reports facts only; it never sets a forecast,
cap, exemption, authority decision or compliance verdict.

### Fixed-base accounting

Measure the retained aggregate once from the immutable package base to the exact combined
candidate/head across all included delivery slices. Count additions plus deletions as gross
changed lines; production and support remain separate and total is their sum. Package patterns
are the evidenced inclusion boundary; changes outside them are unrelated and excluded. Do not
invent ad-hoc exclusions to improve the result.

Renames and deletions remain in the fixed-base result. A rename crossing the approved package
boundary or production/support classification, an uncountable/binary package change, or missing
exact composition/attribution is `UNKNOWN`; do not assert envelope compliance. Splitting,
stacking, rebasing, closing, superseding or landing PRs cannot reset the base or erase retained
changes. A stack or partially landed package must be measured at one exact combined retained
head; per-PR diffs or summed overlapping diffs are not the package aggregate.

### Forecast miss and hard-cap handling

Evaluate production and support independently against their ORIGINAL APPROVED upper forecast.
A forecast-miss trigger fires only when actual retained gross change is both:
- at least 1.5x that original forecast; and
- at least 100 lines over that original forecast.

A forecast miss inside the approved scope/caps is worker-owned: re-estimate and record
**KEEP / SIMPLIFY / REPLACE** before further affected expansion. It does not by itself return
to Marco. **KEEP** requires evidence that the simpler alternative is inadequate; **SIMPLIFY**
removes/reuses mechanisms while preserving the outcome; **REPLACE** adopts a smaller sound
solution and identifies what it supersedes.

For size/envelope reasons, return to Marco only when the current trajectory projects a hard-cap
breach. Bring one credible cut, or a plain reason no safe cut exists. Existing Human Input
triggers remain independent: material scope, architecture, authority/trust, risk/burden or other
consequential changes still return while cheap even if all line caps are green. Existing banned
scope/authority boundaries and the >=500 actual PR/diff exemption rule remain hard and
independent; splitting cannot launder either package growth or the per-PR rule.

### Exceptional LOC exemption gate

Both a governed package hard-cap exemption and a `>=500` actual PR/diff exemption are exceptional.
They are separate decisions: granting either one never grants or weakens the other. Root, the
implementation parent, and the integration owner reject incomplete or routine requests before
they reach Marco. Convenience, sunk cost, schedule pressure, "almost done", ordinary test growth,
or avoiding another PR is not an exceptional reason.

LOC bounds are controls, not optimization targets. Never compress or omit necessary work, choose
499 lines, or otherwise shape a change to sit just below a review boundary. Estimate the smallest
sound implementation honestly and apply every boundary it crosses.

Freeze one complete evidence packet before requesting either exemption. It contains the exact
WorkId and approved package/specification revision; immutable package base and exact candidate/head
where applicable; the reproducible count and disclosed classifications/exclusions; original
forecast, approved margin, current hard cap, exact current count, complete remaining-work forecast,
and one requested absolute final ceiling with explicit contingency; the causal reason the original
plan failed; why the additional lines are essential to the approved outcome; and concrete
`KEEP ONE PR / SPLIT / REPLAN` alternatives, including the strongest smaller design or cut and the
evidence for rejecting it. A legitimate exception must rest on a demonstrated indivisible
correctness, migration, compatibility, recovery or reviewability invariant, a size fixed by an
external contract/artifact, or necessary mechanical movement/deletion for which splitting would
materially reduce reviewability. Merely asserting that the change is atomic is insufficient.

Before the request reaches Marco, two independent forks that did not author, design, or implement
the candidate receive the same frozen packet and independently try to falsify its counts,
classification, unchanged scope, smaller alternatives, atomicity claim, remaining forecast and
contingency, and resistance to split laundering. They challenge and filter; they do not grant the
exemption. The requester must resolve every count, evidence, scope, or viable-smaller-route finding
before escalation. Both reviewers must explicitly `PRE-APPROVE` the same frozen packet; any `HOLD`
blocks escalation, and any material packet revision requires both reviews again. Preserve any
remaining genuine tradeoff dissent verbatim. Only Marco may grant the exemption.

A package-cap request asks once for one absolute replacement ceiling, never an increment such as
"20 more lines". Approval binds the exact WorkId, approved package revision and surfaces. Exceeding
that replacement ceiling requires `STOP` and `CUT / SPLIT / REPLAN`, not another top-up. A later
request is admissible only after genuinely new Marco-directed scope or a new external fact
materially changes the objective; record that as explicit re-scope or a new package rather than
underestimated continuation work. Request the exemption before crossing the cap.

The `>=500` exemption binds the exact stable merge-base/head and measured actual diff and must be
granted before landing. Material candidate or base change invalidates it. Aggregate semantically
coupled stacked and follow-up work serving one objective when testing for laundering. A legitimate
split must be independently useful, valid, reviewable, testable, landable and recoverable/revertible,
without placeholder or dead sibling code. Changing bases, WorkIds, PR boundaries, or line-category
labels does not reset the governing history.

Before approving defensive machinery, exercise the smallest credible implementation at the real
boundary, or use a precise skeleton where execution is not yet practical. Name the contract,
boundary case, expected result and remaining unproved claims. Hypothetical completeness alone
cannot justify additional machinery.

### Existing exact-head review handoff

Supply the governing package projection and exact `package-size.py` report with the exact head.
Review verifies the immutable base, original forecasts, caps, package/support patterns, retained
counts, UNKNOWN state, any forecast-miss disposition and actual PR size. Revised forecasts never
erase an earlier miss or raise a cap. Review each material defensive mechanism against a concrete
contract failure, reproducible fault or credible discriminating boundary case that a simpler
solution would mishandle. Unresolved limits, UNKNOWN composition or an undispositioned trigger
holds only the affected readiness claim; use normal focused rereview, not a second ceremony.

## Test quality and qualification

Prefer the smallest maintainable test set that falsifies the governing behavior and plausible faults. Each material test should have a causal reason to exist; where practical, identify a plausible wrong implementation/fault that the material oracle would reject. Test quantity, assertion count, coverage, or mock realism is not a quality strategy by itself.

Use the highest practical fidelity needed by the claim. Unit/fake/mock evidence is appropriate for local semantics it can truthfully model. Material provider/Git/persistence/subprocess/runtime/external claims require proportionate real or hermetic-boundary evidence when the lower-fidelity test can share a false model. Removing redundant implementation-detail tests is not assurance loss when the causal oracle and required boundary evidence remain.

Qualification is claim-specific and proportionate to consequence, blast radius and practical rollback. Use the cheapest safe evidence that actually exercises the claimed boundary. Do not default every change to a live/external run, and do not let an isolated/mock run establish a claim that depends on live provider/runtime behavior.

Fast causal automated checks remain the baseline. Add behavioral qualification when the claim depends on behavior, at the representative boundary as early as practical. Independent review, automated checks, behavioral qualification and eligible integration work run in parallel unless a genuine dependency makes them serial. Require composition qualification only for an actual integration/composition claim; do not automatically duplicate exact-head proof when no such claim exists.

Keep two claims separate when applicable:
- **LANDING / INERT PROOF:** whether the exact candidate/composition can safely land in its reviewed default-off/inert configuration under the current contract.
- **ACTIVATION / RELIANCE PROOF:** whether enabling or relying on the live external/runtime/user path works safely under the activation contract.

A default-off/inert change may be landing-qualified without live activation when activation is not part of the landing claim. Conversely, landing green never proves activation/reliance. Never substitute activation-only evidence for an unresolved landing consequence, or landing-only evidence for a live activation claim.

Keep deployment artifact identity separate from behavior enablement. Where a seam is needed, prefer a small explicit selector that is default-off; do not build a feature-management platform. Review or Human Review may bound activation but does not authorize it. Within a current direct assignment or explicit `CURRENT` grant for the exact effect and surface, agents own reversible bounded activation, rollback/disable and authoritative readback. High-consequence, destructive, security-, authority-, external-provider-, hard-to-reverse or broad-rollout effects retain their existing Human Review and exact effect-authority requirements.

Named required gate + missing capability, NOT_RUN, SKIP, ambiguous readback or wrong subject identity means that exact claim is not established; it is not candidate failure unless the candidate actually failed, and it is not PASS. Preserve exact-head evidence separately from synthetic/base-composition evidence.

When the target branch advances after an immutable candidate exists, that movement alone does not require rebasing or invalidate exact-candidate review. The Coordinator or integration owner assesses whether leaving the candidate unchanged is safe enough and whether rebasing would materially help delivery. Preserve the exact candidate and its review evidence when current-target composition can be established without rewriting it. Rebase only when needed or materially helpful to resolve conflicts, address semantic interaction with target changes, obtain required CI/composition evidence that cannot otherwise be established, or make delivery materially more practical. A rewritten head is a new exact candidate subject to the current review and evidence rules; keep exact-head and composition identities and evidence distinct either way.

For GitHub-native stacks, authoring and focused layer review may proceed while lower-layer review and CI run. A focused PASS may survive a restack only when the deterministic layer diff and its reviewed dependency/interface contract are both unchanged and no conflict resolution occurred. Otherwise that layer receives focused rereview. Defects are fixed in the lowest semantic owner and cascaded upward. The exact current top/full stack still receives fresh cumulative review and qualification before any included layer lands; V1 does not permit early partial landing. Missing or ambiguous inertness, review identity, or dependency-contract evidence fails closed to the broader current evidence set.

## Code Review

For a substantive candidate, review the exact governing behavior and exact immutable candidate/head. Review depth follows semantic risk, blast radius and practical rollback, not cosmetic size.

Ask in order:
A. **Should this solution exist?** Correct problem and owner? Root fix? Smaller reuse/delete/reframe route?
A1. **Right scale first.** Is this the right thing and scale to build before internal completeness is polished? Does the candidate take the smallest safe useful vertical stage, or justify an independently stable non-speculative foundation plus its immediate route to representative end-to-end proof?
B. **Whole-system effect.** Does local success create duplicate truth, dependency/authority drift, hidden behavior, residue/coupling, disproportionate operational/support burden, or worse agent navigation?
C. **Implementation quality.** Correctness, failure/UNKNOWN semantics, applicable concurrency/idempotency/external semantics, cohesion/readability, unnecessary cleverness; does the implementation still match the concrete owner/reuse/evidence shape of its Execution Plan?
D. **Tests as adversarial artifacts.** What concrete fault does each material test catch? Would plausible broken code fail? Are mocks/fixtures fabricating the real boundary? Is important causal regression or high-fidelity boundary evidence missing? Is the test-support architecture itself becoming a maintenance liability?
E. **Outcome / qualification.** Does the candidate deliver the governing behavior independently of its own tests? Is each landing/activation claim supported by the right evidence subject? Preserve NOT_RUN/SKIP/MISSING_CAPABILITY and exact-head-vs-composition truth.
F. **Scope / decomposition.** Does each PR/layer represent the smallest independently useful, valid, reviewable and recoverable intermediate state? Could an obvious smaller slice safely land with meaningful acceptance evidence and independent rollback/rework? If yes, prefer it. If no, name the reason the behavior/evidence/recovery must remain together. Dependency order alone does not justify bundling; sub-500 splitting does not prove good decomposition; split laundering is a defect.
G. **Documentation currency.** For Implementation Specification-scoped work: verify the candidate's UPDATE/HISTORICALIZE/DELETE documentation-impact dispositions (under 1218218678917862) are reconciled to the actual retained candidate, not the spec's original projection. For work outside that process: does the candidate's diff touch something [architecture](architecture.md) describes? If so, does the same PR update `docs/architecture.md`? Either way, a qualifying diff that leaves its documentation stale is a material finding under Findings, remedies and challenge, not optional polish.

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

ACCEPT_DEFECT_REJECT_REMEDY accepts the defect but does not clear it. The author implements/proposes the smaller authorized clearing route and returns the exact corrected candidate/evidence to the original reviewer for focused rereview against the original defect and minimum clearing condition. The original reviewer answers **WITHDRAW**, **NARROW**, **UPHOLD**, or **NEEDS_EVIDENCE** on that exact claim.

If one material applicability, materiality or clearing dispute genuinely survives that bounded response, Coordinator may acquire one fresh independent reviewer for exact-dispute adjudication only. The request to Marco must plainly state: the exact disputed blocker; why the current reviewer/owner evidence cannot resolve it; the independence or conflict-of-interest property required; and the exact narrow question to decide. There is no automatic third reviewer, Coordinator tie-break review or indefinite review chain. If fresh-review capability is unavailable, preserve the exact affected blocker without inventing another stage. Until cleared or adjudicated, the finding holds only the affected consequential claim/path.

Author/reviewer agreement does not defeat the correction-ratchet reset. If cumulative accepted corrections materially change mechanism, ownership/trust surfaces, implementation envelope, or test/support burden, re-run the whole-current-candidate `should this solution exist? / smaller reuse-delete-reframe?` judgment before another local patch layer.

## Refresh check

For substantive code/config/test implementation, refresh on entry/re-entry/context replacement, before the first material commitment, between distinct material slices, after failed hypotheses/material failures/findings/accepted corrections, before material shape changes, and before readiness/handoff/completion claims.

For substantive Code Review, refresh on entry/re-entry/context replacement, before substantive review, after candidate/evidence/currentness or focused-correction changes, and before verdict/handoff.

At each refresh, reopen this exact candidate/control-SHA version and re-ground only these questions:
1. What exact outcome/non-goals and current canonical owner/reuse seam/Execution Plan shape govern the next work?
2. Has the solution shape materially changed, and is there now a smaller delete/reuse/reframe route?
3. What claim-specific oracle/evidence is still missing, NOT_RUN, SKIPPED, MISSING_CAPABILITY, or UNKNOWN?
4. Is the next material commitment still inside the approved scope/authority/boundary and supported by the current plan/evidence?

This refresh is a reasoning re-ground, not a new approval gate, checklist, timer, or authority source.

## Deterministic and execution truth

The repository's current deterministic quality gates remain whatever the current executable contract/CI actually requires. Never convert NOT_RUN, SKIP, missing capability, wrong-candidate execution, or synthetic-merge/composition evidence into an exact-head PASS claim. Structural/complexity evidence is diagnostic unless an exact current mechanically enforced invariant says otherwise.

This guidance can require plan shape, refresh/replan behavior, test evidence and later review checks; it does not prove the agent actually complied during the coding trajectory. This package contains no deterministic trajectory monitor/evidence gate. Treat conformance during implementation as guidance + readiness/review-backed detection until natural adoption evidence demonstrates behavior. Future mechanical enforcement remains a separately reviewed design problem.

Broader cumulative code-health/audit scheduling belongs to the existing audit owner/process, not this document.
When a Code Audit encounters regression into an oversized staged package, it flags the concrete scale/staging defect and minimum clearing condition; it does not silently redesign the package.
