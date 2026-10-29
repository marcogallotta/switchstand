# Code quality and Code Review

This is the repository semantic owner for substantive code/config/test implementation and Code Review. It complements current active work, approved Design/Implementation Contract, worker-owned Execution Plan, and routed review procedure; it never grants scope, effect, merge, activation, or approval authority.

## Governing standard

A good change delivers the exact governing behavior with the simplest sound current design, lives in the correct owner, preserves explicit authority/trust/resource/dependency boundaries, avoids duplicate truth and speculative machinery, has discriminating maintainable evidence, and leaves the touched system no harder for a fresh competent agent to understand and safely change. Green tests are evidence, not the definition of correctness or quality.

### Root AGENTS admission

Root `AGENTS.md` is a bootstrap/router, not a process manual. Any addition to it must satisfy
all of these in the existing review; this is not a separate ceremony:

1. essentially every fresh agent needs it before role/function selection;
2. omission can cause immediate unsafe or materially misrouted behavior before owner guidance loads;
3. no narrower current semantic owner can carry it;
4. the text is an invariant, first-minute trigger, or routing pointer rather than procedure; and
5. it does not duplicate owner-document semantics.

Any `NO` routes the detail to its current owner and leaves at most the needed trigger/pointer in
root `AGENTS.md`. A serious incident does not automatically qualify a new bootstrap rule. Do not
use a total byte/line cap as the semantic gate: size may be reported diagnostically, but review
must challenge ownership and necessity rather than compress prose to hit a threshold.

## Author / implementer

Prefer the smallest safe vertical end-to-end stage that produces representative useful behavior and evidence. Use a foundation-only or inert first stage only when a vertical stage is unsafe or impractical; state why, keep the foundation independently stable, exclude speculative later-stage machinery, and name the immediate route to the first representative useful end-to-end proof. Default-off or inert readiness is a landing claim, not ordinary-use product completion.

Before material coding:
1. Bind the exact governing outcome, non-goals, current Design/Contract/Plan, candidate/base and writable surfaces. When an approved Human Review baseline governs, record the exact baseline identity and compare the implementation package's scope and ordinary-use consequence as `BASELINE_PRESERVED`, `MATERIAL_DEVIATION`, or `UNKNOWN`; independent design/implementation review validates the comparison before dispatch. `UNKNOWN` blocks only dispatch while minimum evidence, correction, and unaffected authorized work continue.
2. For nontrivial work, make the existing worker-owned Execution Plan concrete enough for a fresh worker to identify: the canonical owner/reuse/delete route; intended changed surfaces and ownership boundaries; the strongest materially smaller credible alternative when the choice is architecture-sensitive; the earliest real/discriminating test or proof; the expected implementation/test-support envelope; and material unknowns. The Execution Plan must also state the proposed PR/layer decomposition and why the chosen cut beats the obvious smaller alternative. The justification is semantic: independently useful/valid behavior, meaningful acceptance evidence, and independent rework/rollback. Line count is not the justification. This is not a second quality plan or a new approval artifact.
3. State a tiny behavioral contract where the claim is material: governing preconditions/inputs, required output/postcondition, relevant failure/UNKNOWN/undefined boundary, and the earliest discriminating example or boundary proof. This is not universal formal-spec or TDD ceremony.
4. Choose the earliest practical evidence capable of falsifying the real claim. Where implementation and tests/mocks can share the same false model — provider, Git, persistence, subprocess, runtime or other external semantics — include proportionate real/hermetic boundary evidence rather than letting the mock world define truth.
5. Treat the expected implementation envelope as an alarm, never as a budget to spend. Large/unexpected mechanism, owner/trust surface, defensive machinery, fixture/support burden or aggregate stack growth is a stop-and-classify signal. Current routed procedure controls any exact hard size/exemption rule.

Implementation Specification authors should include runnable required noninteractive checks using the
[command-block guidance](development.md#noninteractive-commands-from-an-implementation-specification).
When resuming an older specification, add its missing command block with the agreed executable checks
while preserving its acceptance criteria.

For a material change, include a short **test plan in that same Implementation Specification**.
Tie each required check to the approved user outcome and WorkId, a checkable positive result,
and a relevant forbidden or negative effect. Name the earliest causal unit, component, or
integration check, its command and observable result, including a regression for the known fault.
Use the existing command block for runnable noninteractive checks. Add a small scripted end-to-end
journey only when changed behavior crosses a meaningful seam that cheaper checks cannot credibly
cover, such as real MCP/HTTP and persistence, authority/currentness, process composition,
migration, UI, or a critical multi-tool path. No end-to-end or omission boilerplate is universal.
When composition is the claim, launch the candidate through its actual production-shaped entrypoint
with isolated test resources; manually wiring the dependency under test is not that proof.
For small slices, identify the affected fast checks; before a major integrated deployment, name
the applicable broad regression and end-to-end checks on the exact release candidate. Do not
defer an essential seam check until after activation or require a heavy journey after every safe
incremental PR. For each required check, record the proof boundary, expected result, setup and
teardown, rerun condition, cost, and actual `PASS`/`FAIL`/`SKIP`/`NOT_RUN`/`UNKNOWN` result.
Review of a proposed plan is not execution or acceptance evidence.

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

### Independent material judgment

Use a fresh context-free independent native fork for a material judgment about an exception,
overrun, alternative, partial-test/no-change acceptance, or readiness/handoff. Reuse the applicable
existing design, implementation or review stage; routine realization does not need another review.
Actual participation determines independence: an author, designer, implementer or participant in
the disputed disposition cannot independently review it.

The reviewer starts with repository `AGENTS.md`, the exact current WorkId/candidate and raw primary
evidence, follows applicable routes independently, and challenges the question and logic as well
as the proposed answer. A head may supply a proposal and arguments clearly labeled as claims to
check. A brief must not dictate a verdict, replace primary evidence with its summary, narrow away
relevant challenge, or suppress a counterargument. Within the assigned judgment, inspect the
evidence and applicable guidance needed to assess incomplete framing; bounded-worker scope is
not authority for the head to forbid that challenge. Missing admitted evidence becomes a concrete
evidence request to the parent, never guessed truth or unrelated discovery.

Return causal evidence, credible alternatives, uncertainty and recommendation. The responsible
Coordinator owns disposition within current authority; review grants no extension or effect
authority. For a concrete unresolved question, expertise gap or evidence conflict, the reviewer
may recommend a second independent reviewer with the exact reason and question. The parent
arranges that bounded review; no automatic recursive chain or mandatory pair for every exception.
Preserve material dissent and follow the existing finding/dispute route.

### Material design review before Human Review

Start the first existing material-design review when there is a concrete provisional route,
before substantial detailed-design investment. Material work receives one fresh context-free
reviewer before Human Review. Use the same review occurrence through specification; this is not
an additional clearance or a reason to
delay useful early Human Input. Routine realization inside settled direction is unaffected.

First give the reviewer current work identity, original/current human direction, binding
constraints and authority, and the relevant current owner/source references. Before seeing the
author's preferred design, rationale or prior overall verdict, the reviewer independently
checks current capability, ownership/overlap, the useful remaining gap and the smallest credible
benefit-preserving route. Keep this investigation bounded to the proposed material work.

Then compare the exact author proposal/design against that independent assessment. Challenge
necessity and V1 scope before technical completeness, including owner placement, the strongest
smaller route, evidence shape and any material human choice. The author fixes or answers material
findings before final Human Review. Later material deltas receive focused checking in that same
review, not an automatic full restart or another reviewer. Cheap research and unaffected work
continue; hold only a commitment that consumes an unresolved material choice or lacks authority.
Review supplies evidence, never implementation, landing, activation or provider-effect authority.
The same independent pre-implementation review challenges the Specification and its test plan:
the reviewer invents a plausible wrong result and asks whether a proposed check would fail,
whether end-to-end coverage is needed for an uncovered seam, and whether any proposed journey
adds no value. Use that existing review and focused rereview, not a second test-plan gate.

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

### Coordinator extension review

Approved aggregate production, support and total caps remain separate review triggers. Before
further affected expansion would exceed an approved ceiling, stop that expansion and bring the
causal reason to the responsible Coordinator. A breach never grants automatic permission, even
below 2x. Reconcile already-observed overruns before continuing; unaffected authorized work proceeds.

Keep the ORIGINAL approved baseline caps and immutable package base. For each applicable dimension,
record its original cap, current fixed-base retained count, complete remaining forecast, proposed
absolute final ceiling and any prior extension. The Coordinator may approve a justified extension
for the SAME approved task only when every proposed extended ceiling is strictly below twice its
corresponding ORIGINAL approved cap. A later extension gets a new causal review against that same
original baseline, never the last extension. A zero original cap cannot become a positive allowance
by multiplication. Missing baseline/counts or UNKNOWN composition cannot establish this route.
At 2x or above in ANY breached dimension, take the prepared decision to Marco before further
affected expansion. A total below 2x does not clear a support or production dimension at 2x;
unused allowance in one dimension does not transfer to another.

Review WHY the cost grew: necessary work for the approved outcome, underestimated implementation
or assurance cost, a changed external fact, or invented scope/mechanism. Use a fresh independent
judgment under the section above; Coordinator approval remains a separate reasoned disposition.
Challenge scope creep at any multiplier. An estimate miss or necessary test growth can justify
same-task continuation when evidence supports it; convenience, sunk cost, schedule pressure and
"almost done" alone do not. Do not remove useful approved behavior, necessary causal tests or
readability just to make the count fit.

The existing task/Execution Plan carries exact WorkId, approved baseline/package revision,
base/head, reproducible production/support/total counts, causal growth explanation, remaining
forecast, credible KEEP ONE PR / SPLIT / SIMPLIFY / REPLACE routes and their engineering benefit
or functional loss, independent judgment, and Coordinator approval/challenge/escalation with
absolute ceilings. Approval binds that same scope and those ceilings. Crossing an extended ceiling
triggers review again; it never compounds the original denominator or silently admits new scope.
This replaces the mandatory two-reviewer aggregate-extension permission stage.

Existing human choices about outcome, scope, architecture, trust/authority, risk or burden still
return while cheap, at any count. Preserve current HOLD/DENIED, independent Code Review, CI,
exact-head/composition, landing and separate deployment/activation authority.

### Exceptional LOC exemption gate

The per-PR rule is separate from aggregate task caps. Aim for coherent PRs around 300 gross changed
lines as a norm, not another hard gate. Agents split independently useful, reviewable PRs without
asking Marco, keeping required related tests with the relevant behavior. Splitting never resets
the aggregate baseline, hides cumulative growth or admits scope creep. An aggregate package above
500 lines is not itself a `>=500` PR exception; measure each actual PR against its exact stable
merge-base/head separately from the fixed-base retained package.

A `>=500` actual PR/diff remains on **HOLD** absent an exact Marco exception before landing. Such
requests should basically never be necessary: first attempt coherent smaller delivery boundaries
and explain the concrete technical inability to split safely. An indivisible correctness,
migration, compatibility, recovery or reviewability constraint needs evidence; convenience,
deadlines, laziness, already-written code, sunk cost, ordinary test growth or avoiding another PR
do not qualify. Do not cut approved functionality or synthetically slice invalid partial states.
Use the existing independent review and canonical challenge below; no mandatory second reviewer
unless a concrete unresolved reason requires one. Coordinator may approve an aggregate extension
within the rule above, but cannot waive this distinct per-PR ceiling. Conversely, splitting a large
PR needs no human permission and cannot waive an aggregate cap.

LOC bounds are controls, not optimization targets. Never compress or omit necessary work, choose
499 lines, or otherwise shape a change to sit just below a review boundary. Estimate the smallest
sound implementation honestly and apply every boundary it crosses.

Use existing task/Execution Plan evidence for an extension or per-PR exception. It contains the exact
WorkId and approved package/specification revision; immutable package base and exact candidate/head
where applicable; the reproducible count and disclosed classifications/exclusions; original
forecast, approved margin, original cap, approved ceiling, exact count, complete remaining forecast,
and one requested absolute final ceiling with explicit contingency; the causal reason the original
plan failed; why the additional lines are essential to the approved outcome; and concrete
`KEEP ONE PR / SPLIT / REPLAN` alternatives, including the strongest smaller design or cut and the
evidence for rejecting it. A legitimate per-PR exception must rest on a demonstrated indivisible
correctness, migration, compatibility, recovery or reviewability invariant, a size fixed by an
external contract/artifact, or necessary mechanical movement/deletion for which splitting would
materially reduce reviewability. Merely asserting that the change is atomic is insufficient.

### Canonical exception challenge

Give each reviewer this section by reference as part of the current review package; do not quote,
summarize, or rewrite it in the request. The fresh independent reviewer applies this complete
challenge, testing both the head's framing and the proposed answer:

1. Keep the per-PR exception on `HOLD` pending evidence. Reconstruct the original outcome,
   immutable base and original limits without accepting candidate-created invariants as requirements.
2. Verify the cumulative fixed-base counts, classification, remaining forecast and resistance to
   PR, WorkId, rebase, category, or serial-increment laundering.
3. Produce and test the strongest credible `CUT / SPLIT / SIMPLIFY / REPLACE` route. Reject sunk
   cost, convenience, schedule pressure, ordinary test growth, and "almost done" as reasons.
4. Do not force a misleading or unsafe split, omit necessary tests, compress readable code, or aim
   for 499 lines. State the concrete correctness, migration, compatibility, recovery, or
   reviewability harm caused by the best smaller route.
5. Return `HOLD` unless the evidence defeats that smaller route. State the evidence, the attempted
   smaller route, why it fails, and whether the unchanged packet is genuinely exceptional.

The requester resolves every count, evidence, scope or viable-smaller-route finding; a surviving
material blocker holds only its affected claim. Material revisions receive focused rereview.
Apply Coordinator extension authority only to aggregate caps, never as a per-PR waiver. A `>=500`
PR exception reaches Marco only after concrete evidence establishes why sound splitting fails;
review agreement does not grant it. Preserve genuine tradeoff dissent verbatim.

An aggregate extension requests absolute ceilings measured against the immutable original limits,
never a renewed multiplier on the last extension. Same-task underestimate continuation follows
Coordinator causal review below 2x; at 2x or above it goes to Marco. Genuinely new Marco-directed
scope requires explicit re-scope, never laundering as an underestimated continuation.

The `>=500` exemption binds the exact stable merge-base/head and measured actual diff and must be
granted before landing. Material candidate or base change invalidates it. Check coupled stacked
and follow-up work against the aggregate baseline; do not sum legitimate independent PRs to invent
a per-PR exception. A legitimate
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

For GitHub-native stacks, authoring and focused layer review may proceed while lower-layer review and CI run. A focused PASS may survive a restack only when the deterministic layer diff and its reviewed dependency/interface contract are both unchanged and no conflict resolution occurred. Otherwise that layer receives focused rereview. Defects are fixed in the lowest semantic owner and cascaded upward. The ordinary path requires fresh cumulative review and qualification of the exact current top/full stack before any included layer lands; explicit-human fasttrack above defers that review while retaining qualification. V1 does not permit early partial landing. Missing or ambiguous inertness, review identity, or dependency-contract evidence fails closed to the broader current evidence set.

## Explicit human fasttrack

When Marco explicitly says to fasttrack the scoped change, that direction authorizes deferring
independent Code Review until after deployment. Record it on the existing exact WorkId; do not
ask him to repeat the deferral or approve the same scope again. Urgency alone is not fasttrack.
The deferral covers the pre-landing review wait too, so landing does not recreate the removed
pre-deploy gate. It supersedes a hold caused solely by waiting for that review. Ordinary changes
keep their existing review gates.

The responsible Coordinator carries the scoped candidate through this path:

1. Bind Marco's direction, the exact immutable candidate and current target/composition, the
   deployment surface and current effect authority on the existing work. A direction such as
   “fasttrack this to prod” can supply both deferral and that scoped deployment authority;
   deferral alone does not invent a deployment, activation, credential or provider-effect grant.
2. Require successful current CI and every required E2E check for that candidate and the claimed
   boundary. Preserve exact-head versus composition evidence. FAIL, NOT_RUN, SKIP,
   MISSING_CAPABILITY, UNKNOWN or stale/wrong-candidate evidence does not satisfy a required
   check. Use the existing claim-specific qualification contract; do not invent universal live
   tests, a second CI path, or a blanket test bypass.
3. Record the known-good rollback/disable target and practical recovery action, then land and
   deploy the tested revision within current authority. Verify the installed revision and the
   required live behavior through the existing activation/readback procedure. A changed candidate
   or composition needs corresponding qualification; a review deferral is not evidence transfer.
4. Own the deferred independent review of the exact deployed revision. Record the review owner,
   next checkpoint and any remaining obligation on the same work before deployment; the reviewer
   need not have completed, approved, or even started review before deployment. Dispatch promptly
   after deployment, retain findings and dispositions, and carry corrections or justified
   challenges through focused rereview. Keep review visibly open until terminal evidence or an
   explicit attributable handoff with pickup; deployment success is not review completion.

A missed review checkpoint triggers inquiry and recovery of the obligation, not automatic
rollback. An actual defect calls for proportionate correction, disablement or rollback according
to consequence and current authority; live failures use the incident procedure. Neither reviewer
availability nor a newly invented risk category may restore the waived pre-deploy review gate.
Current distinct human holds, platform denials and technical impossibility still apply.

Migrations remain separately authorized and governed; fasttrack does not waive their requirements.
Rollback restores code/configuration only where the actual recovery path supports it: it cannot
undo disclosed data, already-sent external actions or irreversible writes. Identify such concrete
effects before acting and reconcile their exact authority and any genuinely unresolved human
choice. Do not turn that check into a blanket prohibition on fasttracking ordinary reversible
changes. Record the actual deployment/readback and deferred-review outcome separately.

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
H. **Approved-baseline conformance.** When a Human Review baseline governs, bind the exact candidate and baseline and return `CONFORMS_TO_APPROVED_BASELINE` only when protected requirements/non-goals remain intact, no deviation remains, and no new human-owned outcome, experience, architecture, trust/data boundary, burden, material risk, or protected effect was introduced. A conforming candidate needs no second Human Review solely because it is complete. `MATERIAL_DEVIATION`, `NEW_HUMAN_CHOICE`, or `UNKNOWN` holds only the affected readiness claim; landing and activation authority remain separate.

Every approving or clearing review also checks this danger hotlist:
- **D1 WRONG OUTCOME** — technically successful work solves a different problem from Marco's current outcome.
- **D2 INVENTED SCOPE** — a new requirement, blocker, restriction, or capability lacks current authority.
- **D3 WAR MACHINE / OVERENGINEERING** — machinery precedes a working slice that proves its need; name its concrete cost.
- **D4 ANTI-MALICE OVERBUILD** — agents can fail but are not malicious; do not assume internal-agent malice without an explicit current threat or direction.
- **D5 SECURITY LAUNDERING** — “security” grants no authority; name the actor, attack path/capability, and material consequence.
- **D6 DUPLICATE / SHADOW MACHINERY** — a second owner or path exists where one canonical path plus a local guard suffices.
- **D7 COMPLEXITY / EXTENSION RATCHET** — aggregate growth lacks causal review against original caps or compounds an extension; a >=500 PR lacks the concrete smaller-route challenge and exact exception. Splitting does not erase growth.
- **D8 SAFEGUARD-GENERATED HARM** — protection removes required capability, blocks ordinary use, creates disproportionate burden, or makes Marco the routine control plane.
- **D9 FALSE / PROXY SUCCESS** — PASS rests on proxy evidence while the real path, identity, or readback remains unproved.
- **D10 UNAUTHORIZED REMOVAL / DEPLOYMENT** — removal/disablement and production deployment/activation require Marco's explicit go; calling something “security,” removing functions, then deploying without approval fires D5 + D10.
A fire is a cue, not automatic blocker or remedy authority: record evidence, consequence, affected path, and minimum clearing condition; then use the smallest authorized fix/remove/reject/evidence route and focused rereview. Automatically resolved or clear cases produce no Marco noise; only a surviving genuine Human Input choice or separately protected effect reaches him with evidence, smaller routes tried, recommendation, and exact consequences. Otherwise report `DANGER CHECK: CLEAR`.

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
