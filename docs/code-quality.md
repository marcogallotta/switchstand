# Code quality and Code Review

This owns substantive code/config/test implementation and Code Review alongside assigned work,
its approved Design/Contract and worker Execution Plan; it grants no scope, effect or approval authority.

Use applicable AGENTS phase routes. Authors use [Author](#author--implementer),
[qualification](#test-quality-and-qualification), [refresh](#refresh-check) and
[execution truth](#deterministic-and-execution-truth); reviewers also use [Code Review](#code-review)
and [Findings](#findings-remedies-and-challenge). [Cleanup](#cleanup-prove-less-work-to-understand-and-change)
loads for cleanup work. Material design or independent material judgment also loads
[independence](#independent-material-judgment) and [design review](#material-design-review-before-human-review);
material Design/Implementation Specifications include [scope blocks](#mandatory-scope-blocks), even outside packages.
Governed work uses [package controls](#governed-implementation-packages) and their accounting/handoff routes.
[Forecast/cap](#forecast-miss-and-hard-cap-handling), [PR-size](#exceptional-loc-exemption-gate),
[root admission](#root-agents-admission) and [fasttrack](#explicit-human-fasttrack) rules load at their named triggers.
Follow embedded owner links. This index makes no applicable requirement optional; UNKNOWN applicability
requires reading its owner before the affected decision.

## Governing standard

A good change delivers the exact governing behavior with the simplest sound current design, lives in the correct owner, preserves explicit authority/trust/resource/dependency boundaries, avoids duplicate truth and speculative machinery, has discriminating maintainable evidence, and leaves the touched system no harder for a fresh competent agent to understand and safely change. Green tests are evidence, not the definition of correctness or quality.

### Cleanup: prove less work to understand and change

The target is maintainable, proportionate software, not a numeric grade. For an authorized cleanup,
use five practical outcomes; [research](agent-code-and-test-quality-research.md) supplies evidence,
not another procedure:

- **Discoverable:** a fresh agent with the task and repository finds the correct owner, relevant
  docs and test command. Record time and wrong turns rather than assuming links prove usability.
- **Cohesive code:** rules have authoritative owners; inspect the hardest flows and identify
  unnecessary edits across unrelated files. Moving complexity into helpers or wrappers is not enough.
- **Proportionate scope:** retained mechanisms serve Marco's approved V1 needs or necessary contracts.
  Internal callers show dependencies, not product need. Remove unnecessary machinery rather than relocate it.
- **Valuable, economical tests:** realistic faults are rejected at the relevant boundary. Reduce
  redundant cases, expensive fixtures and coupled mocks while preserving unique regression protection.
- **Fast, safe changes:** representative fresh-agent work needs less investigation, test overhead
  and rework, with correctness and authority safeguards preserved.

Start with one approved candidate or bounded subsystem, its baseline SHA, V1 outcome/non-goals and
current [Mailbox](mailbox.md) and [Tracker](tracker.md) owner maps. [Observed use](how-marco-uses-switchstand.md#current-observed-use-and-continuity-limit-marco-2026-10-06)
is evidence, not signed scope. Keep settled approvals; unknown usage does not authorize retirement.
For a material unresolved product/scale choice, prepare the credible smaller route and use existing
[Human Input](human-input.md). Resolve engineering facts without making Marco investigate the code.

Briefly record material touched owners/consumers, expensive change paths, affected tests and important
unknowns in existing Work/plan. Choose KEEP, CUT, MERGE or RETIRE from actual source/contract evidence.
For each significant simplification, show one concrete before/after example: which decision,
mechanism, coordinated edits, fixtures or obsolete docs disappear, what remains authoritative, and
which realistic regression would fail. Group tests by protected behavior; no per-item inventory or
fault declaration for every test. Green CI, deleted LOC and test counts alone establish no quality.
Use counts/runtime only where they explain the observed burden.

Periodically compare matched representative maintenance tasks on exact baseline/candidate revisions
in disposable writers, with fresh agents and comparable briefs/context/tools. Record owner discovery,
first meaningful regression, correct change/review time, rework, files touched and human interruptions;
include failures, sample size and confounders. There is no universal research-backed time limit:
benchmark first, then agree useful budgets with Marco. These experiments inform major milestones,
not every PR. Missing trials mean speed UNMEASURED; do not extrapolate sampled gains to the whole system.

When declaring a subsystem cleanup complete, use the existing independent review, not an additional
approval stage. The reviewer verifies current, trustworthy evidence of a prior fresh agent finding,
changing and testing the code; repeat the exercise only when something important remains unproven.
Check its provenance and applicability; author assertions, stale trials and self-review do not qualify.
The reviewer examines hard paths, duplicate rules, scope, tests and docs, and compares baseline burden
and safety against the five outcomes. Return PASS, FAIL or NOT_ESTABLISHED with material findings,
covered journeys and remaining uncertainty.
Minor style preferences do not block completion. Known serious defects fail the affected claim;
missing material evidence leaves completion NOT_ESTABLISHED, not a fabricated defect or blanket PASS.
The current programme is limited to Mailbox and Tracker until both pass. Marco directs scope and
progression by command; there is no general progression rule or automatic next subsystem. Reevaluate
the method with him after these two. Stop unproductive
cuts and report remaining gaps or scope questions to Marco; do not broaden scope or add cuts to obtain a PASS.

Preserve authority, identity/currentness, replay/idempotency, truthful UNKNOWN, fencing, transaction
atomicity and recovery. State plausible failures, relevant real/hermetic evidence, residual risk and
recovery; tests cannot prove absent consumers or undo external effects. Ordinary small PRs retain
existing review/qualification; local/native review does not satisfy an explicitly required external
review. PASS grants no merge, deployment or continuation authority. The human-controlled
[per-PR LOC gate](#exceptional-loc-exemption-gate) and applicable governed-package controls are unchanged.

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

Prefer the smallest safe vertical end-to-end stage with representative useful behavior and evidence.
If that is unsafe or impractical, explain why a foundation-only/inert stage is independently stable,
exclude speculative later-stage machinery, and name the immediate route to the first useful proof.
Inert readiness is a landing claim, not ordinary-use completion.

Before material coding:
1. Bind the governing outcome/non-goals, current Design/Contract/Plan, candidate/base and writable surfaces. When Human Review governs, complete the exact pre-dispatch baseline comparison under [baseline preservation and deviation](human-review.md#baseline-preservation-and-deviation); UNKNOWN holds only dispatch while evidence/correction and unaffected work continue.
2. For nontrivial work, make the worker-owned Execution Plan identify the canonical owner/reuse/delete
   route, changed surfaces/boundaries, strongest materially smaller architecture-sensitive alternative,
   earliest discriminating proof, implementation/test-support envelope and material unknowns.
   Justify PR/layer decomposition by independently useful/valid behavior, meaningful acceptance evidence
   and independent rework/rollback, not line count. Use the existing plan, not a second artifact.
3. For a material claim, state a tiny behavioral contract: preconditions/inputs, required postcondition,
   failure/UNKNOWN/undefined boundary and earliest discriminating example. This is not universal TDD
   or formal-spec ceremony. Choose the earliest practical falsification under
   [Test quality and qualification](#test-quality-and-qualification).
4. Treat the envelope as an alarm, not a budget: unexpected mechanism, owner/trust surface, defensive
   machinery, fixture burden or aggregate growth triggers the stop/replan below. Exact hard limits
   remain in [governed packages](#governed-implementation-packages).

Implementation Specification authors should include runnable required noninteractive checks using the
[command-block guidance](development.md#noninteractive-commands-from-an-implementation-specification).
When resuming an older specification, add its missing block with agreed executable checks while
preserving acceptance criteria.

During implementation:
- Fix the causal owner, not callers/tests around it; preserve one truth, explicit boundaries and
  discoverable behavior, failure/UNKNOWN semantics, executable contracts and relevant docs. Avoid
  hidden global effects, duplicate policy/state/config/currentness and convenience authorities.
- At each [refresh](#refresh-check), compare the trajectory with the Execution Plan. An unexpected
  owner/provider/persistence/trust/interface/external-I/O/recovery surface, materially larger
  defensive/test-support machinery or loss of planned proof means stop/replan/classify the affected
  path before another patch layer. Routine local/private/reversible mechanics remain worker-owned.
- If work materially outgrows or invalidates the planned PR/layer shape, the parent/integration owner
  (sole implementer without a parent) chooses **KEEP ONE PR / SPLIT / REPLAN** before further expansion.
  KEEP ONE PR when behavior/evidence/recovery form one coherent reviewable unit and splitting gives a
  misleading/invalid partial state. SPLIT for a smaller independently useful, valid, reviewable,
  recoverable slice with meaningful acceptance evidence and independent rollback/rework; identify
  why it is better. REPLAN for changed mechanism, owner/trust boundary, evidence or recovery.
  Do not defer this decision to Code Review or split for a threshold; escalate only across an
  existing authority/review/exemption boundary.
- Preserve demonstrated causal regression cases when recutting a mechanism, not accidental scaffolding.
  Do not change the oracle to make code pass. Test-support choreography or invented external semantics
  can be a maintenance defect even with little production code; use the test-quality rules below.
- If accumulated local fixes materially change mechanism, ownership/trust, implementation envelope
  or test/support burden, reset the whole-solution judgment: should it still exist, or can it be
  deleted/reused/reframed? Author/reviewer agreement does not waive this reset.
- Existing debt never authorizes new local debt.

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

Use a fresh context-free independent native fork for a material exception, overrun, alternative,
partial-test/no-change acceptance or readiness/handoff judgment in the existing applicable stage.
Routine realization needs no extra review. Actual participation determines independence: authors,
designers, implementers and participants in the disputed disposition cannot independently review it.

The reviewer starts with `AGENTS.md`, exact current WorkId/candidate and raw primary evidence,
follows applicable routes, and challenges framing and logic as well as the answer. Supplied
proposals/arguments are claims to check, never a dictated verdict or substitute for evidence.
Bounded scope cannot forbid relevant challenge, suppress counterarguments or narrow away incomplete
framing. Request missing admitted evidence from the parent; do not guess or expand into unrelated discovery.

Return causal evidence, credible alternatives, uncertainty and recommendation. The responsible
Coordinator owns disposition within current authority; review grants no extension or effect
authority. For a concrete unresolved question, expertise gap or evidence conflict, the reviewer
may recommend a second independent reviewer with the exact reason and question. The parent
arranges that bounded review; no automatic recursive chain or mandatory pair for every exception.
Preserve material dissent and follow the existing finding/dispute route.

### Material design review before Human Review

Start the first existing material-design review on a concrete provisional route before substantial
detailed-design investment. Material work receives one fresh context-free reviewer before Human Review;
use the same occurrence through specification, without delaying useful early
[Human Input](human-input.md). Routine realization inside settled direction is unaffected.

First supply work identity, original/current human direction, binding constraints/authority and
current owner/source references. Before the preferred design, rationale or prior overall verdict,
the reviewer independently checks current capability, overlap/ownership, useful gap and smallest
credible benefit-preserving route within the proposed material work. Then compare the exact proposal
for necessity/V1 scope, owner placement, strongest smaller route, evidence and material human choice.
Resolve material findings before final [Human Review](human-review.md). Later material deltas receive
focused checking in that review, not an automatic restart/reviewer. Continue cheap research and
unaffected work; hold only commitments consuming an unresolved choice or lacking authority.

### Mandatory scope blocks

Before Human Review, every material Design Specification records repository surfaces/patterns;
original production/config and support forecasts; production, support and total hard caps; V1 binary
allowance of zero changed files/bytes; counts of new tables, tools, services, processes, flags, grant
operations, persistent stores/state machines and runtime-config crossings; strongest materially
smaller route; and exact breach/replan condition. Missing/unresolved fields mean **NOT READY**.

Before dispatch, the material Implementation Specification reconciles those fields, freezes the
immutable package base, declares patterns covering every base-to-final-head changed path (support
patterns only classify covered paths), copies original forecasts/caps, records the exact
`package-size.py` command and names one cumulative package/integration owner. Missing fields or
unresolved `UNKNOWN` mean **NOT READY**. Apply the accounting below without resetting the projection.

Repository LOC and external effects are separate. Notes/receipt updates are outside the Git report,
not a second counter or cap reset. For included effects bind exact existing WorkIds/fields, preserve
unrelated text, use exact CAS/operation receipts, reread resulting text/revisions, and replan broader
durable surface/policy effects. Landing never proves process activation.

### Fixed-base accounting

Recompute retained additions plus deletions as gross changed lines from the immutable package base to one exact combined
head across all delivery slices; production and support are separate and total is their sum.
Use `python scripts/package-size.py` with that base/head, package/support patterns, original forecasts
and approved caps, not a running line ledger. The script reports facts, not forecasts, caps,
exemptions, authority or compliance. Forecasts are alarms, not targets; re-estimates cannot erase a
miss, reset the original denominator or raise an approved cap.

The base must be an ancestor of the combined head. Package patterns define evidenced inclusion;
exclude unrelated paths outside them, never ad-hoc exclusions. Any uncovered path means `UNKNOWN`
without cap comparison. Added/deleted/content-changed binaries mean `UNKNOWN`; V1 allows only a
Git-detected pure rename with identical old/new blob OIDs at zero gross. Nonzero binary allowances
remain deferred. Renames/deletions remain counted; a rename across package boundary or
production/support classification, uncountable change or missing exact composition/attribution
means `UNKNOWN`, not envelope compliance.

Splitting, stacking, rebasing, closing, superseding, landing, WorkId or line-category changes cannot
reset the base, original forecasts/caps or governing history, nor erase retained changes. Measure a
stack/partly landed package at one exact combined retained head, not per-PR or summed overlapping diffs.

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

Before further affected expansion exceeds an approved production, support or total ceiling, stop
and bring the causal reason to the responsible Coordinator. Reconcile observed overruns before
continuing; a breach grants no permission, even below 2x. Unaffected authorized work proceeds.

For each dimension record its ORIGINAL cap, fixed-base retained count, complete remaining forecast,
proposed absolute final ceiling and prior extensions. Coordinator may extend the SAME approved task
only when every ceiling is strictly below twice its corresponding ORIGINAL approved cap. Later
extensions receive fresh causal review against the original baseline, never the last extension.
A zero cap cannot become positive by multiplication; missing baseline/counts or UNKNOWN composition
cannot establish this route. At 2x or above in ANY breached dimension, take the prepared decision to
Marco before further affected expansion. Lower total does not clear a 2x production/support dimension;
allowances do not transfer.

Use [independent material judgment](#independent-material-judgment) to challenge WHY cost grew:
necessary approved work, underestimated implementation/assurance, changed external fact or invented
scope/mechanism. Coordinator approval is a separate reasoned disposition. Challenge scope creep at
any multiplier. Evidence can justify necessary test growth/same-task underestimate; convenience,
sunk cost, schedule pressure or "almost done" cannot. Preserve useful approved behavior, necessary
causal tests and readability rather than cutting them to fit.

Record the current [package projection](#governed-implementation-packages) and [fixed-base count report](#fixed-base-accounting), growth cause, complete remaining forecast, prior extensions, KEEP ONE PR / SPLIT / SIMPLIFY / REPLACE alternatives with benefit/loss, independent judgment and Coordinator disposition. Include current approved and requested absolute ceilings with explicit contingency. Approval binds scope/ceilings; crossing an extended ceiling requires review again. New Marco-directed scope requires explicit re-scope.

Human choices about outcome, scope, architecture, trust/authority, risk or burden return while cheap
at any count. Preserve current HOLD/DENIED, independent Code Review, CI, exact-head/composition, landing
and separate deployment/activation authority. Coordinator extension never waives the per-PR gate below.

### Exceptional LOC exemption gate

Aim for coherent PRs around 300 gross changed lines as a norm, not a hard gate. Agents split
independently useful, reviewable PRs without asking Marco, keeping related required tests with the
behavior. An aggregate package above 500 lines is not itself a `>=500` PR exception: measure each
actual PR at its exact stable merge-base/head, separately from fixed-base package accounting.

A `>=500` actual PR/diff remains on **HOLD** absent an exact Marco exception before landing. First
attempt coherent smaller delivery boundaries and the [canonical challenge](#canonical-exception-challenge).
Requests should basically never be necessary. Coordinator cannot waive this ceiling; splitting needs
no human permission but cannot waive package caps. Use the existing independent review, not a
mandatory second reviewer without a concrete unresolved reason.

An exception requires a demonstrated indivisible correctness, migration, compatibility, recovery or
reviewability invariant, externally fixed contract/artifact size, or necessary mechanical
movement/deletion whose splitting would materially reduce reviewability. Merely asserting atomicity,
deadlines, laziness, already-written code or avoiding another PR is insufficient; the challenge
below rejects convenience, sunk cost and ordinary test growth. Never cut approved functionality,
force invalid partial states, compress/omit necessary work or choose 499 lines to avoid a boundary.
Estimate the smallest sound implementation honestly and apply every crossed control.

Use the existing [package projection](#governed-implementation-packages) and [fixed-base count report](#fixed-base-accounting), plus the exact actual-PR merge-base/head and reproducible count/classification/exclusions. State current ceiling, complete remaining forecast, requested absolute final ceiling and contingency, why the original plan failed, why growth is essential, and the best KEEP ONE PR / SPLIT / REPLAN and smaller-design alternatives with rejecting evidence. The exception binds that measured diff; material candidate/base change invalidates it. Check coupled follow-ups against aggregate history without summing independent PRs into an invented exception. Any split must be useful, valid, reviewable, testable, landable and recoverable independently, without placeholder/dead sibling code.

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

Before approving defensive machinery, exercise the smallest credible implementation at the real
boundary, or use a precise skeleton where execution is not yet practical. Name the contract,
boundary case, expected result and remaining unproved claims. Hypothetical completeness alone
cannot justify additional machinery.

### Existing exact-head review handoff

Supply the current [package projection](#governed-implementation-packages) and exact-head [count report](#fixed-base-accounting). Review their fields, forecast-miss disposition and actual PR size. Each material defensive mechanism needs a concrete failure or discriminating boundary case the smaller solution mishandles. Unresolved limits/composition/triggers hold affected readiness only; use focused rereview.

## Test quality and qualification

Use the smallest maintainable set falsifying governing behavior and plausible faults. Each material
test needs a causal reason; where practical name a wrong implementation it rejects. Quantity,
assertion count, coverage and mock realism alone are not quality. Test invariants, not private call
sequencing; removing redundant implementation-detail tests loses no assurance when causal oracles
and required boundary evidence remain.

Match fidelity to the claim: unit/fake/mock evidence may establish local semantics it truthfully
models. Provider/Git/persistence/subprocess/runtime/external claims need proportionate real or
hermetic evidence when mocks can share the same false model. Use the cheapest safe evidence that
exercises the boundary, proportionate to consequence, blast radius and rollback; neither default
every change to live tests nor use mock-only proof for live-dependent behavior.

Fast causal automated checks remain the baseline. Add behavioral qualification when the claim depends on behavior, at the representative boundary as early as practical. Independent review, automated checks, behavioral qualification and eligible integration work run in parallel unless a genuine dependency makes them serial. Require composition qualification only for an actual integration/composition claim; do not automatically duplicate exact-head proof when no such claim exists.

Keep **LANDING / INERT PROOF** (safe landing of the reviewed default-off/inert candidate/composition)
separate from **ACTIVATION / RELIANCE PROOF** (safe live external/runtime/user use). Neither establishes
the other. Artifact identity is distinct from enablement; prefer a small explicit default-off
selector when needed, not a feature-management platform. Review/Human Review may bound activation,
not authorize it. Current direct assignment or explicit `CURRENT` grant for the exact surface/effect
permits bounded reversible activation, rollback/disable and authoritative readback. High-consequence,
destructive, security-, authority-, external-provider-, hard-to-reverse or broad-rollout effects
retain existing Human Review and exact effect-authority requirements.

A required gate with missing capability, NOT_RUN, SKIP, ambiguous readback or wrong subject identity
is unestablished, not candidate failure unless it actually failed, and never PASS. Keep exact-head
and synthetic/base-composition evidence distinct; [development](development.md#match-evidence-to-the-claim)
owns operational examples.

Target-branch movement alone does not require rebasing or invalidate immutable-candidate review.
Coordinator/integration owner assesses safety and delivery benefit: retain candidate/evidence when
current-target composition can be established without rewriting. Rebase only for needed/materially
helpful conflict or semantic-interaction resolution, unavailable required CI/composition proof, or
delivery practicality. A rewritten head is a new exact review/evidence subject.

GitHub-native layer authoring/focused review may overlap lower-layer review/CI. A focused PASS survives
restacking only when the deterministic layer diff AND reviewed dependency/interface contract are
unchanged with no conflict resolution; otherwise focused rereview. Fix the lowest semantic owner and
cascade upward. Before any included layer lands, ordinary V1 requires fresh cumulative review and
qualification of the exact current top/full stack, no early partial landing. Explicit-human fasttrack
below defers review, not qualification. Ambiguous/missing inertness, review identity or dependency
contract fails closed to the broader current evidence set.

## Explicit human fasttrack

Marco's explicit fasttrack of a scoped change defers independent Code Review until after deployment,
including the pre-landing wait. Record his direction on the exact existing WorkId and continue;
do not ask him to repeat it. Urgency is not fasttrack. It supersedes holds caused solely by that
review wait; ordinary changes keep review gates.

The responsible Coordinator:
1. Binds direction, immutable candidate, current target/composition, deployment surface and effect
   authority. "Fasttrack this to prod" can supply deferral and scoped deployment authority;
   deferral alone invents no activation, credential, deployment or provider-effect grant.
2. Requires successful current CI and every required E2E check to pass for the exact claimed boundary under
   [qualification](#test-quality-and-qualification). Retain exact-head/composition truth and all
   FAIL/NOT_RUN/SKIP/MISSING_CAPABILITY/UNKNOWN/stale distinctions; no blanket bypass, universal
   live tests or second CI path.
3. Records known-good rollback/disable target and recovery action, lands/deploys the tested revision
   within authority, then verifies installed revision and required live behavior via the existing
   activation/readback procedure. Changed candidate/composition needs corresponding qualification;
   deferral transfers no evidence.
4. Before deployment records the deferred exact-deployed-revision review owner, checkpoint and
   obligations on the same Work. The reviewer need not have started or approved before deployment.
   Dispatch promptly afterward; retain findings/dispositions and carry corrections/justified
   challenges through focused rereview. Keep it open until terminal evidence or attributable handoff
   with pickup. Record deployment/readback separately from review outcome.

A missed checkpoint triggers inquiry/recovery, not automatic rollback. A real defect calls for
proportionate correction, disablement or rollback within authority; live failures use the incident
procedure. Reviewer availability or an invented risk category cannot restore the waived gate.
Current distinct human holds, platform denials and technical impossibility still apply.

Migrations retain separate authorization/requirements. Rollback cannot undo disclosed data, sent
external actions or irreversible writes; identify concrete effects and reconcile authority and
unresolved human choices before acting. This is not a blanket prohibition on ordinary reversible
fasttrack changes.

## Code Review

Review the exact governing behavior and immutable candidate/head. Depth follows semantic risk,
blast radius and practical rollback, not cosmetic size. Ask in order:

A. **Should this solution exist?** Correct problem, causal owner, smaller reuse/delete/reframe route?
A1. **Right scale first.** Does it follow the Author's smallest useful stage rule, including the
    non-speculative foundation exception and immediate end-to-end proof?
B. **Whole-system effect.** Duplicate truth, dependency/authority drift, hidden behavior, coupling,
   residue, disproportionate support burden or worse fresh-agent navigation?
C. **Implementation quality.** Correctness, failure/UNKNOWN, concurrency/idempotency/external semantics,
   cohesion/readability and unnecessary cleverness; does it still match its Execution Plan?
D. **Tests as adversarial artifacts.** Apply [test quality](#test-quality-and-qualification): concrete
   faults rejected, causal/high-fidelity gaps, fabricated mock boundaries or costly support architecture?
E. **Outcome / qualification.** Behavior established independently of its own tests? Are each
   landing/activation claim and its exact-head/composition subject supported by the right evidence?
F. **Scope / decomposition.** Apply the Author's KEEP ONE PR / SPLIT / REPLAN rule: prefer an obvious
   smaller useful/valid/reviewable/recoverable slice with acceptance evidence and independent rework;
   otherwise name why behavior/evidence/recovery must stay together. Dependency order or sub-500
   slicing alone is no justification; split laundering is a defect.
G. **Documentation currency.** For Implementation Specification work, reconcile UPDATE/HISTORICALIZE/DELETE
   dispositions (under 1218218678917862) to the actual retained candidate. Otherwise, if the diff touches
   something architecture describes, update `docs/architecture.md` in the same PR. Stale qualifying
   documentation is a material finding, not optional polish.
H. **Approved-baseline conformance.** When a Human Review baseline governs, bind it to the exact
   candidate and apply [Human Review: baseline preservation and deviation](human-review.md#baseline-preservation-and-deviation).
   Return `CONFORMS_TO_APPROVED_BASELINE`, `MATERIAL_DEVIATION`, `NEW_HUMAN_CHOICE` or `UNKNOWN`
   against that contract. A conforming build needs no repeat Human Review solely for completion;
   landing and activation authority remain separate.

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

A material finding names the exact defect/invariant, evidence, consequence/materiality, affected
claim/path and minimum clearing condition. The remedy is advisory unless the governing Design/Contract
requires it. The author dispositions it:
- **ACCEPT**: valid defect; choose the smallest authorized clearing fix.
- **ACCEPT_DEFECT_REJECT_REMEDY**: valid defect, over-scoped remedy; supply a smaller clearing route.
- **CHALLENGE**: dispute applicability/materiality/causality/evidence with exact counterevidence.
- **NEEDS_EVIDENCE**: the claim cannot yet be established safely.

A valid blocker holds only the affected consequential claim/path; unaffected authorized work continues.
ACCEPT_DEFECT_REJECT_REMEDY does not clear it: propose/implement the smaller authorized route and return
the exact corrected candidate/evidence to the original reviewer against the original defect/minimum
clearing condition. That reviewer answers **WITHDRAW**, **NARROW**, **UPHOLD**, or **NEEDS_EVIDENCE**.

If a material applicability/materiality/clearing dispute survives that bounded response, Coordinator
may acquire one fresh independent reviewer for exact-dispute adjudication only. The request to Marco
states the exact disputed blocker, why current reviewer/owner evidence cannot resolve it, the required
independence/conflict-of-interest property and exact narrow question. There is no automatic third
reviewer, Coordinator tie-break or indefinite chain. Missing fresh-review capability preserves the
affected blocker without inventing another stage until cleared/adjudicated.

Apply the Author's whole-solution reset to cumulative accepted corrections before another local patch layer.

## Refresh check

For substantive implementation, refresh on entry/re-entry/context replacement, before first material
commitment, between material slices, after failed hypotheses/material failures/findings/accepted
corrections, before material shape changes and before readiness/handoff/completion claims.
For substantive Code Review, refresh on entry/re-entry/context replacement, before review, after
candidate/evidence/currentness/focused-correction changes and before verdict/handoff.

Reopen this exact candidate/control-SHA version and re-ground:
1. Exact outcome/non-goals, canonical owner/reuse seam and Execution Plan governing next work?
2. Materially changed shape; now a smaller delete/reuse/reframe route?
3. Missing claim-specific oracle/evidence, NOT_RUN, SKIPPED, MISSING_CAPABILITY or UNKNOWN?
4. Next commitment still within approved scope/authority/boundary and current plan/evidence?

This is reasoning re-grounding, not a new gate, checklist, timer or authority source.

## Deterministic and execution truth

Current executable contract/CI owns deterministic gates; structural/complexity evidence is diagnostic
unless an exact mechanically enforced invariant says otherwise. Preserve the qualification states above.
This guidance has no deterministic trajectory monitor: plan/refresh/test/review requirements do not
prove implementation followed them. Treat trajectory conformance as guidance plus readiness/review
detection until natural adoption evidence exists; future mechanical enforcement is a separate design.
Cumulative code-health/audit scheduling retains its existing owner. Code Audit flags oversized staging
with the concrete defect/minimum clearing condition, not a silent package redesign.
