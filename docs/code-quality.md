# Code quality and Code Review

This owns substantive code/config/test implementation and Code Review alongside assigned work, its approved Design/Contract and worker Execution Plan; it grants no scope, effect, merge, activation or approval authority.

Use existing AGENTS phase routes and the applicable sections below, including embedded owner links. No applicable rule becomes optional; UNKNOWN applicability requires reading its owner before the affected decision. Material-design and independent-judgment rules apply whether or not a governed package is present.

## Governing standard

Deliver the exact governing behavior with the simplest sound design and correct owner. Preserve authority, trust, resource and dependency boundaries; avoid duplicate truth and speculative machinery. Require discriminating, maintainable evidence and leave the system no harder for a fresh competent agent to understand and safely change. Green tests alone establish neither correctness nor quality.

### Cleanup: prove less work to understand and change

Authorized cleanup targets maintainable, proportionate software, not a numeric grade. Use five outcomes; [research](agent-code-and-test-quality-research.md) supports them without adding procedure:

- **Discoverable:** a fresh agent finds owners, relevant docs and test commands; record discovery time and wrong turns, not merely links.
- **Cohesive code:** rules have authoritative owners; inspect hard flows for unnecessary coordinated edits. Helpers/wrappers alone do not remove complexity.
- **Proportionate scope:** retain approved V1 needs and necessary contracts. Internal callers establish dependencies, not product need; remove unnecessary machinery rather than relocate it.
- **Valuable, economical tests:** reject realistic faults at the relevant boundary while reducing redundant cases, costly fixtures and coupled mocks; preserve unique regression protection.
- **Fast, safe changes:** reduce investigation, test overhead and rework in representative fresh-agent maintenance while preserving correctness and authority safeguards.

Start with one approved candidate/bounded subsystem, exact baseline SHA, V1 outcome/non-goals and current [Mailbox](mailbox.md)/[Tracker](tracker.md) owner maps. [Observed use](how-marco-uses-switchstand.md#current-observed-use-and-continuity-limit-marco-2026-10-06) is evidence, not signed scope or retirement authority. Preserve settled approvals; unknown use does not authorize deletion. Resolve engineering facts locally; take a material unresolved product/scale choice and credible smaller route through [Human Input](human-input.md).

In existing Work/plan, record material owners/consumers, expensive change paths, affected tests and unknowns. Choose KEEP/CUT/MERGE/RETIRE from source/contracts. For each significant simplification, show what decision, mechanism, coordinated edits, fixtures or obsolete docs disappear, the remaining authoritative owner, and a realistic regression that would fail. Group tests by behavior, without per-test inventories/fault declarations. CI, deleted LOC and test counts prove no quality by themselves; counts/runtime explain observed burden only.

Periodically compare matched maintenance tasks on exact baseline/candidate revisions in disposable writers, using fresh agents and comparable briefs/context/tools. Record discovery, first meaningful regression, correct change/review time, rework, files touched and human interruptions, including failures, sample size and confounders. These experiments inform major milestones, not every PR. Benchmark before agreeing useful budgets with Marco; there is no universal research-backed time limit. Missing trials mean speed UNMEASURED; sampled gains do not generalize to the whole system.

For subsystem completion, use the existing independent review. Verify current, trustworthy provenance/applicability of prior fresh-agent finding/changing/testing evidence; repeat only important unproved portions. Author assertions, stale trials and self-review do not qualify. Inspect hard paths, duplicate rules, scope, tests/docs and baseline burden/safety against all five outcomes. Return PASS/FAIL/NOT_ESTABLISHED with covered journeys, material findings and uncertainty: a known serious defect fails the affected claim; missing material evidence is NOT_ESTABLISHED; minor style preferences do not block.

The programme remains Mailbox and Tracker until both pass. Marco commands progression; no automatic next subsystem. Reevaluate the method with Marco after these two; stop unproductive cuts and report gaps/scope questions rather than broaden scope or cut merely to earn PASS.

Preserve authority, identity/currentness, replay/idempotency, truthful UNKNOWN, fencing, atomicity and recovery. State plausible failures, real/hermetic evidence, residual risk and recovery; tests prove neither absent consumers nor reversal of external effects. Ordinary small PRs retain existing review/qualification; native/local review cannot substitute for an explicitly required external review. PASS grants no continuation/merge/deploy authority; package controls and the [per-PR gate](#exceptional-loc-exemption-gate) remain.

### Root AGENTS admission

Root `AGENTS.md` is a bootstrap/router, not a process manual. In the existing review, every addition must satisfy all five conditions:

1. essentially every fresh agent needs it before role/function selection;
2. omission risks immediate unsafe/materially misrouted behavior before owner guidance loads;
3. no narrower current semantic owner can carry it;
4. it is an invariant, first-minute trigger, or routing pointer rather than procedure;
5. it does not duplicate owner-document semantics.

Any NO leaves detail in its owner and at most the necessary root trigger/pointer. An incident alone is no admission reason. Do not use a total byte/line cap as the semantic gate; size is diagnostic, while ownership and necessity govern.

## Author / implementer

Prefer the smallest safe vertical end-to-end stage with representative useful behavior/evidence. If unsafe/impractical, justify an independently stable, foundation-only/inert stage, exclude speculative later-stage machinery and name the immediate route to the first useful end-to-end proof. Inert readiness is a landing claim, not ordinary-use completion.

Before material coding:

1. Bind outcome/non-goals, current Design/Contract/Plan, candidate/base and writable surfaces. For a governing Human Review baseline, record exact identity and BASELINE_PRESERVED/MATERIAL_DEVIATION/UNKNOWN; independent design/implementation review validates it before dispatch. UNKNOWN holds dispatch only while minimum evidence, correction and unaffected authorized work continue. Follow [baseline preservation and deviation](human-review.md#baseline-preservation-and-deviation).
2. For nontrivial work, the worker's existing Execution Plan identifies canonical owner/reuse/delete route, changed surfaces/boundaries, strongest materially smaller architecture-sensitive alternative, earliest discriminating proof, implementation/test-support envelope and material unknowns. Justify layers/PRs by independently useful valid behavior, meaningful acceptance evidence and independent rework/rollback, not line count or another artifact.
3. For material claims, state preconditions/inputs, required postcondition, failure/UNKNOWN/undefined boundary and earliest discriminating example. This tiny behavioral contract is not universal TDD/formal specification; use the earliest practical [falsification](#test-quality-and-qualification).
4. Treat forecasts as alarms, not budgets; unexpected mechanism, owner/trust surface, defensive machinery, fixture burden or aggregate growth triggers replan. [Package controls](#governed-implementation-packages) retain hard limits.

Implementation Specification authors should include runnable required noninteractive checks through [command-block guidance](development.md#noninteractive-commands-from-an-implementation-specification). Add an older specification's missing block using agreed checks without weakening acceptance.

During implementation:

- Fix the causal owner, preserving one truth, explicit boundaries, discoverable behavior, failure/UNKNOWN, executable contracts and relevant docs. Avoid hidden global effects and duplicate policy/state/config/currentness or convenience authority.
- At each [refresh](#refresh-check), compare with the plan. Unexpected owner/provider/persistence/trust/interface/external-I/O/recovery surfaces, materially larger defensive/test machinery or lost proof require stop/replan/classification before another patch layer. Routine private/local/reversible mechanics remain worker-owned.
- If PR/layer shape materially outgrows or invalidates the plan, the parent/integration owner (sole implementer without a parent) chooses **KEEP ONE PR / SPLIT / REPLAN** before expansion. KEEP when behavior/evidence/recovery form one coherent reviewable unit and splitting misleads or invalidates it. SPLIT when a smaller independently useful, valid, reviewable, recoverable slice offers meaningful acceptance and independent rework/rollback; explain its advantage. REPLAN changed mechanism, ownership/trust, evidence or recovery. Do not defer the choice to review or split for thresholds; escalate only at existing authority/review/exemption boundaries.
- Retain causal regressions, not accidental scaffolding; never change the oracle merely to pass. Test-support choreography/invented external semantics can be maintenance defects even with little production code.
- Accumulated changes to mechanism, ownership/trust, envelope or support burden reset whole-solution judgment: delete, reuse or reframe if better. Author/reviewer agreement does not waive this reset; existing debt authorizes no new local debt.

## Material design and independent judgment

### Independent material judgment

Use a fresh context-free independent native fork for material exceptions, overruns, alternatives, partial-test/no-change acceptance or readiness/handoff judgment in the existing stage. Routine realization adds no review. Authors/designers/implementers and participants in the disputed disposition are ineligible; participation, not labels, determines independence.

Start from AGENTS, exact current WorkId/candidate and raw primary evidence, then applicable routes. Challenge framing and logic; supplied proposals/verdicts are claims, not instructions. Bounded scope cannot suppress relevant challenge/counterarguments or incomplete framing. Request missing admitted evidence from the parent without guessing or unrelated discovery.

Return causal evidence, credible alternatives, uncertainty and recommendation; Coordinator dispositions remain bounded by current authority. Review grants no extension/effect. A concrete unresolved question, expertise gap or evidence conflict can justify a parent-arranged second reviewer with exact reason/question, never an automatic pair/recursive chain. Preserve material dissent through [findings](#findings-remedies-and-challenge).

### Material design review before Human Review

Start the existing material-design review on a concrete provisional route before substantial design investment. Material work receives one fresh context-free reviewer before Human Review; retain the same occurrence through specification, alongside early [Human Input](human-input.md). Routine realization within settled direction is unaffected.

Supply identity, original/current human direction, constraints/authority and current owner/source references first. Before seeing preferred design/rationale/prior verdict, the reviewer checks capabilities, overlap/ownership, useful gap and smallest credible benefit-preserving route. Then assess the exact proposal's necessity, V1 scope, owner, smaller alternative, evidence and material human choice. Resolve material findings before [Human Review](human-review.md); later material deltas get focused checking, not a review restart. Cheap research/unaffected work continue; hold commitments consuming unresolved choices or lacking authority.

### Mandatory scope blocks

Before Human Review, every material Design Specification records surfaces/patterns, original production/config and support forecasts, production, support and total caps, V1 binary allowance of zero files/bytes, counts of new tables, tools, services, processes, flags, grant operations, persistent stores, state machines and runtime-config crossings, strongest materially smaller route and exact breach/replan condition. Missing/unresolved fields mean **NOT READY**.

Before dispatch, the material Implementation Specification reconciles these fields, freezes immutable base, declares patterns covering every base-to-final changed path (support patterns classify covered paths only), copies original forecasts/caps, records the exact `package-size.py` command and names one cumulative package/integration owner. Missing fields/unresolved UNKNOWN mean **NOT READY**; apply accounting without resetting the projection.

Repository LOC and external effects remain separate. Notes/receipts are outside the Git report, not a second counter/cap reset. Included effects bind exact WorkIds/fields, preserve unrelated text, use exact CAS/operation receipts and reread resulting text/revisions. Broader durable surfaces/policy effects require replan; landing proves no activation.

## Governed implementation packages

For a governed implementation task, keep one compact package projection in the existing
task/Execution Plan. It carries the approved record; it is not a second approval artifact,
package database, or parallel review ceremony. At approval freeze:
- package identity, exact approved record revision and immutable package base SHA;
- included package surfaces and which of them are support (tests/fixtures/tools/docs);
- the ORIGINAL APPROVED upper forecasts for production and support;
- the explicit approved margins and resulting production, support and total hard caps;
- the strongest credible simpler alternative, economy proof and material replan triggers.

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

Record on the existing task/Execution Plan: exact WorkId and approved baseline/package revision,
base/head, reproducible production/support/total counts, growth cause, remaining forecast, credible
KEEP ONE PR / SPLIT / SIMPLIFY / REPLACE routes and engineering benefit/functional loss, independent
judgment and Coordinator approval/challenge/escalation with absolute ceilings. Include approved
margin, current approved ceiling and requested final ceiling with explicit contingency. Approval
binds that scope/ceilings; crossing an extended ceiling triggers review again. This replaces the
mandatory two-reviewer aggregate-extension permission stage. Genuinely new Marco-directed scope
requires explicit re-scope, not an underestimated-continuation label.

Human choices about outcome, scope, architecture, trust/authority, risk or burden return while cheap
at any count. Preserve current HOLD/DENIED, independent Code Review, CI, exact-head/composition, landing
and separate deployment/activation authority. Coordinator extension never waives the per-PR gate below.

### Existing exact-head review handoff

Supply the governing projection and exact `package-size.py` report with the exact head. Review the
[accounting](#fixed-base-accounting), patterns/counts, original forecasts/caps, UNKNOWN, forecast-miss
disposition and actual PR size. For each material defensive mechanism require a concrete contract
failure, reproducible fault or discriminating boundary case a simpler solution mishandles.
Unresolved limits/composition or undispositioned triggers hold only affected readiness; use focused
rereview, not another ceremony.

## Exceptional LOC exemption gate

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

Use existing task/Execution Plan evidence: exact WorkId and approved package/specification revision,
base/head where applicable, reproducible counts and disclosed classifications/exclusions, original
forecast/margin/cap, approved ceiling, exact count, complete remaining forecast and requested absolute
final ceiling with explicit contingency. Explain why the original plan failed, why the added lines
are essential and concrete KEEP ONE PR / SPLIT / REPLAN alternatives, strongest smaller design/cut
and evidence rejecting it. The exception binds the exact stable merge-base/head and measured actual
diff; a material candidate/base change invalidates it. Check coupled stack/follow-up work against the
aggregate baseline; do not sum legitimate independent PRs to invent a per-PR exception. A split must
be independently useful, valid, reviewable, testable, landable and recoverable/revertible, without
placeholder/dead sibling code.

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

For substantive implementation, refresh on entry/re-entry/context replacement, before first material commitment, between slices, after failed hypotheses/material failures/findings/accepted corrections, before material shape changes and readiness/handoff/completion claims. For review, refresh on entry/re-entry/context replacement, before review, after candidate/evidence/currentness/focused corrections and before verdict/handoff.

Reopen exact candidate/control SHA and re-ground outcome/non-goals, owner/reuse seam and governing plan; changed shape and smaller delete, reuse or reframe route; missing oracle/evidence and NOT_RUN/SKIPPED/MISSING_CAPABILITY/UNKNOWN; and scope/authority/boundaries for next commitment. This is reasoning, not a new gate, checklist, timer or authority.

## Deterministic and execution truth

Executable contracts/CI own deterministic gates. Structural/complexity evidence is diagnostic absent an exact enforced invariant; retain qualification distinctions. No deterministic trajectory monitor exists: plan/refresh/review requirements do not prove conformance. Until natural adoption evidence, guidance plus readiness/review detects deviations; enforcement is separate design. Existing owner retains cumulative audit scheduling; Code Audit names concrete oversized-staging defect/minimum clearing condition without silently redesigning the package.
