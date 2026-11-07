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

Keep one compact projection in the existing task/Execution Plan, carrying the approved record without a second approval artifact, database or ceremony. At approval freeze record:

- package identity, exact approved record revision and immutable base SHA;
- included surfaces and support classification (tests/fixtures/tools/docs);
- ORIGINAL APPROVED production and support upper forecasts;
- approved margins and resulting production, support and total hard caps;
- strongest credible smaller alternative, economy proof and material replan triggers.

The stage rules below reuse this projection and the fixed-base report; they never reset approved history.

### Fixed-base accounting

Measure retained additions plus deletions from immutable base to one combined exact head across all slices; production and support are separate and total their sum. Use `python scripts/package-size.py` with base/head, package/support patterns, original forecasts and approved caps, not a running ledger. It reports facts, not forecasts, caps, exemptions, authority or compliance. Re-estimates cannot erase misses, reset denominators or raise approved caps.

Require base ancestry. Patterns establish inclusion; no ad-hoc exclusions. Uncovered paths mean UNKNOWN without cap comparison. Added/deleted/content-changed binaries mean UNKNOWN: V1 allows only Git-detected pure renames with identical old/new blob OIDs at zero gross; nonzero allowances are deferred. Count renames/deletions; cross-package/classification renames, uncountable changes or missing exact composition/attribution mean UNKNOWN, not compliance.

Splitting, stacking, rebasing, closing, superseding or landing, WorkId or category changes never reset base, original forecasts/caps/history or erase retained changes. Measure partly landed stacks/packages at one combined retained head, not per-PR or summed overlapping diffs.

### Forecast miss and hard-cap handling

Evaluate production and support against their ORIGINAL APPROVED upper forecasts independently. Forecast miss requires BOTH at least 1.5x original forecast AND at least 100 lines over it.

Inside approved scope/caps, the worker re-estimates and records **KEEP / SIMPLIFY / REPLACE** before affected expansion, without automatically returning to Marco. KEEP proves the smaller route inadequate; SIMPLIFY removes/reuses while preserving outcome; REPLACE names a smaller sound successor and what it supersedes.

### Coordinator extension review

Before further affected expansion exceeds an approved production, support or total cap, stop the affected path and bring the causal reason to Coordinator. Reconcile existing overruns before continuation: breach grants no permission, even below 2x. Unaffected authorized work continues.

For each dimension retain ORIGINAL cap, fixed-base count, complete remaining forecast, requested absolute ceiling and prior extensions. Coordinator may extend the SAME approved task only when every ceiling is strictly below twice its corresponding ORIGINAL approved cap. Each extension gets fresh review against original baseline, never the last extension. Zero caps cannot become positive; missing baseline/counts or UNKNOWN composition establish no extension. At 2x or above in ANY breached dimension, take the prepared decision to Marco before expansion. Lower total does not clear a 2x breach in either production or support; allowances do not transfer.

Use [independent judgment](#independent-material-judgment) on WHY growth occurred: necessary approved work, underestimated implementation/assurance, changed external fact or invented scope/mechanism. Coordinator approval is separate. Challenge scope creep at any multiplier; convenience, sunk cost, schedule pressure and “almost done” justify no growth. Preserve approved useful behavior, necessary causal tests and readability rather than cutting to fit.

On the existing task/plan record projection/count report, growth cause, complete remaining forecast, KEEP ONE PR / SPLIT / SIMPLIFY / REPLACE alternatives and benefit/loss, independent judgment and Coordinator approval/challenge/escalation. Include original margin, current/requested absolute ceilings and contingency. Approval binds scope/ceilings; crossing an extension requires review again. This supersedes the mandatory two-reviewer aggregate-extension stage. New Marco-directed scope requires explicit re-scope.

Human-owned outcome, scope, architecture, trust/authority, risk and burden choices return while cheap at any count. Preserve HOLD/DENIED, independent Code Review, CI, exact-head/composition, landing and separate deployment/activation authority. Coordinator cannot waive the per-PR gate.

### Existing exact-head review handoff

Supply projection and exact `package-size.py` report with exact head. Review fixed-base accounting, patterns/counts, original forecasts/caps, UNKNOWN, forecast-miss disposition and actual PR size. Each material defensive mechanism needs a concrete contract failure, reproducible fault or discriminating boundary case a smaller route mishandles. Unresolved limits/composition/triggers hold affected readiness only; focused rereview adds no ceremony.

## Exceptional LOC exemption gate

Aim around 300 gross changed lines per coherent PR as a norm, not a hard gate. Agents split independently useful, reviewable PRs without asking Marco, retaining required tests with behavior. An aggregate package above 500 lines is not itself a `>=500` PR exception: measure each actual PR at exact stable merge-base/head, separately from package accounting.

A `>=500` actual PR/diff remains on **HOLD** absent an exact Marco exception before landing. First try coherent smaller boundaries and the [canonical challenge](#canonical-exception-challenge); requests should basically never be needed. Coordinator cannot waive this ceiling. Splitting needs no permission and waives no package cap; use existing independent review, not a mandatory second reviewer without concrete reason.

Exceptions require demonstrated indivisible correctness, migration, compatibility, recovery or reviewability, an externally fixed contract/artifact size, or necessary mechanical movement/deletion whose split would materially reduce reviewability. Assertions of atomicity, deadlines, laziness, sunk code or avoiding another PR do not qualify. Never cut approved behavior, force invalid partial states, omit necessary work/tests, compress readability or aim for 499; estimate the smallest sound solution honestly and apply every crossed control.

Use projection/count evidence, exact WorkId and approved package/spec revision, base/head, reproducible counts/classifications/exclusions, original forecast/margin/cap, current ceiling, complete remaining forecast and requested absolute ceiling/contingency. Explain plan failure, essential growth and KEEP ONE PR / SPLIT / REPLAN alternatives, strongest smaller cut/design and rejecting evidence. Exception binds measured stable merge-base/head; material base/candidate change invalidates it. Check coupled follow-ups against aggregate history without summing independent PRs into invented exceptions. Splits must be independently useful, valid, reviewable, testable, landable and recoverable, without placeholder/dead siblings.

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

Resolve count, evidence, scope or viable-smaller-route findings before the affected claim advances; material blockers hold that claim only, with focused rereview for material changes. Aggregate extension is never a per-PR waiver. A >=500 exception reaches Marco only when sound splitting is concretely defeated; review agreement does not grant it. Preserve genuine tradeoff dissent verbatim.

Before approving defensive machinery, exercise the smallest credible implementation at the real boundary, or a precise skeleton if execution is impractical. Name contract, boundary case, expected result and unproved claims; hypothetical completeness justifies no extra machinery.

## Test quality and qualification

Use the smallest maintainable set falsifying governing behavior/plausible faults. Each material test needs a causal reason and, where practical, a wrong implementation it rejects. Quantity/assertions/coverage/mock realism prove no quality. Test invariants, not private sequencing; redundant detail tests can disappear when causal oracles/required boundary proof remain.

Match fidelity to the claim. Unit, fake or mock evidence establishes only modeled local semantics. Provider, Git, persistence, subprocess, runtime and external claims need proportionate real/hermetic evidence when mocks can share a false model. Use cheapest safe boundary evidence matched to consequence, blast radius and rollback, without default live tests or mock-only live proof.

Fast causal checks remain baseline. Add representative behavioral qualification early when behavior matters. Independent review, automated checks, behavioral qualification and eligible integration work run in parallel unless a genuine dependency makes them serial. Require composition qualification only for an actual integration/composition claim; do not automatically repeat exact-head proof without one.

**LANDING / INERT PROOF** covers safe reviewed default-off/inert candidate/composition landing; **ACTIVATION / RELIANCE PROOF** covers live external/runtime/user use. Neither proves the other; artifact identity is not enablement. Prefer a small explicit default-off selector, not a feature platform. Review/Human Review may bound activation but never authorize it. Current direct assignment or exact-surface/effect CURRENT grant permits bounded reversible activation, rollback/disable and authoritative readback. High-consequence, destructive, security/authority/external-provider, hard-to-reverse or broad-rollout effects retain Human Review/exact effect authority.

Missing capability, NOT_RUN, SKIP, ambiguous readback or wrong subject leaves a required gate unestablished, not failed unless it failed, never PASS. Keep exact-head and synthetic/base-composition evidence distinct; [development](development.md#match-evidence-to-the-claim) owns examples.

Target-branch movement alone does not require rebasing or invalidate immutable-candidate review. Coordinator/integration owner weighs safety and delivery benefit: retain candidate/evidence if current-target composition proves safety without rewriting. Rebase for needed/materially helpful conflict/semantic resolution, unavailable required CI/composition proof or delivery practicality. Rewritten head means new exact subject.

GitHub-native authoring/focused layer review may overlap lower-layer review/CI. Focused PASS survives restack only with unchanged deterministic diff AND reviewed dependency/interface contract, and no conflict resolution; otherwise focused rereview. Fix lowest semantic owner and cascade upward. Ordinary V1 requires fresh cumulative review/qualification of current exact top/full stack before any layer lands: no early partial landing. Explicit fasttrack defers review only. Missing/ambiguous inertness, review identity or dependency contract fails closed to broader current evidence.

## Explicit human fasttrack

Marco's explicit scoped fasttrack defers independent Code Review until after deployment, including pre-landing waits. Record exact WorkId direction and continue without repeat permission. Urgency is insufficient; only holds solely due to review wait are superseded. Ordinary review gates remain.

Coordinator:

1. Bind direction, immutable candidate, target/composition, deployment surface and effect authority. “Fasttrack this to prod” can grant scoped deployment plus deferral; deferral alone grants no activation, credentials, deployment or provider effect.
2. Require current successful CI and all required E2E for the [claimed boundary](#test-quality-and-qualification). Preserve exact-head/composition and FAIL/NOT_RUN/SKIP/MISSING_CAPABILITY/UNKNOWN/stale distinctions, without blanket bypass, universal live tests or second CI path.
3. Record known-good rollback/disable target and recovery; land/deploy the tested revision within authority, then verify installed revision and required live behavior using existing activation/readback procedure. Changed subject requires new corresponding qualification; deferral transfers no evidence.
4. Before deploying, record exact-deployed-revision deferred review owner, checkpoint and obligations on the same Work. Reviewer need not have started/approved. Dispatch promptly afterward, retain findings/dispositions and corrections/justified challenges through focused rereview until terminal or attributable handoff with pickup. Record deployment/readback separately.

Missed checkpoint means inquiry/recovery, not automatic rollback. Actual defects warrant proportionate authorized correction/disable/rollback; live failures use incident procedure. Reviewer availability/invented risk categories cannot restore the waived wait. Distinct human holds, platform denials and technical impossibility still apply.

Migrations retain separate requirements/authority. Rollback cannot undo disclosed data, sent actions or irreversible writes; identify effects and reconcile authority/unresolved choices before acting. Ordinary reversible fasttrack is not blanket-prohibited.

## Code Review

Review governing behavior and immutable candidate/head, with depth set by semantic risk, blast radius and practical rollback, not cosmetic size. Ask in order:

A. **Should this solution exist?** Correct problem/causal owner; smaller reuse/delete/reframe route?
A1. **Right scale first.** Apply Author's smallest useful stage and non-speculative foundation/first-proof exception.
B. **Whole-system effect.** Duplicate truth, authority/dependency drift, hidden behavior, coupling/residue, support burden or worse fresh navigation?
C. **Implementation quality.** Correctness, failure/UNKNOWN, concurrency/idempotency/external semantics, cohesion/readability, needless cleverness and plan fidelity?
D. **Tests as adversarial artifacts.** Apply [test quality](#test-quality-and-qualification): faults rejected, causal/fidelity gaps, fabricated mocks or costly support architecture?
E. **Outcome / qualification.** Behavior established beyond its own tests; exact-head/composition evidence appropriate to each landing/activation claim?
F. **Scope / decomposition.** Apply Author's KEEP ONE PR / SPLIT / REPLAN. Prefer useful/valid/reviewable/recoverable smaller slices with acceptance and independent rework; otherwise justify unified behavior/evidence/recovery. Dependency order/sub-500 slicing alone is no justification; split laundering is a defect.
G. **Documentation currency.** Implementation Specifications reconcile UPDATE/HISTORICALIZE/DELETE dispositions under 1218218678917862 to retained candidate; otherwise update architecture.md in the same PR when touched behavior is described there. Stale qualifying docs are material, not polish.
H. **Approved-baseline conformance.** When a Human Review baseline governs, bind exact candidate/baseline and apply [Human Review](human-review.md#baseline-preservation-and-deviation). Return CONFORMS_TO_APPROVED_BASELINE only for intact requirements/non-goals, no unresolved deviation and no new/material human-owned outcome, experience, architecture, trust/data boundary, burden, risk or protected effect. Otherwise return MATERIAL_DEVIATION/NEW_HUMAN_CHOICE/UNKNOWN, holding affected readiness only. Conforming completion needs no repeat Human Review; landing/activation authority remains separate.

Every approving/clearing review checks the danger hotlist:

- **D1 WRONG OUTCOME:** technically successful solution misses Marco's current outcome.
- **D2 INVENTED SCOPE:** new requirement/blocker/restriction/capability lacks authority.
- **D3 WAR MACHINE / OVERENGINEERING:** machinery precedes proof from a working slice; name cost.
- **D4 ANTI-MALICE OVERBUILD:** internal-agent malice is not assumed without explicit threat/direction.
- **D5 SECURITY LAUNDERING:** security grants no authority; name actor, capability/attack path and consequence.
- **D6 DUPLICATE / SHADOW MACHINERY:** second owner/path where one canonical path/local guard suffices.
- **D7 COMPLEXITY / EXTENSION RATCHET:** growth lacks original-cap causal review, compounds extensions or lacks >=500 smaller-route challenge/exact exception; splits erase no growth.
- **D8 SAFEGUARD-GENERATED HARM:** protection removes required capability, blocks ordinary use, adds disproportionate burden or makes Marco routine control plane.
- **D9 FALSE / PROXY SUCCESS:** proxy PASS leaves real path/identity/readback unproved.
- **D10 UNAUTHORIZED REMOVAL / DEPLOYMENT:** removal/disablement and production deployment/activation need Marco's explicit go; “security” removal then unapproved deployment fires D5+D10.

A fire is a cue, not an automatic blocker or remedy authority. Record evidence and consequence, affected path and minimum clearing condition; pursue smallest authorized fix/remove/reject/evidence and focused rereview. Clear/automatic resolutions need no Marco interruption. Surviving human choices/protected effects reach him with evidence, smaller routes, recommendation and exact consequences; otherwise report DANGER CHECK: CLEAR.

PASS means materially sufficient for the exact claim, not perfect or effect approval. Style/nits do not serialize work.

## Findings, remedies and challenge

Material findings identify exact defect/invariant, evidence, consequence/materiality, affected claim/path and minimum clearing condition. Remedy is advisory unless required by governing Design/Contract. Author dispositions:

- **ACCEPT:** valid defect; smallest authorized fix.
- **ACCEPT_DEFECT_REJECT_REMEDY:** valid defect, oversized remedy; propose smaller clearing route.
- **CHALLENGE:** exact counterevidence disputes applicability/materiality/causality/evidence.
- **NEEDS_EVIDENCE:** claim not safely established.

Block only the affected consequential claim/path. ACCEPT_DEFECT_REJECT_REMEDY does not clear it: implement/propose smaller authorized fix, return exact candidate/evidence to original reviewer against original defect/clearing condition. Reviewer answers WITHDRAW, NARROW, UPHOLD or NEEDS_EVIDENCE.

For surviving material applicability/materiality/clearing dispute, Coordinator may obtain one fresh independent reviewer solely for exact-dispute adjudication. Request to Marco names exact disputed blocker, why owner/reviewer evidence cannot resolve it, required independence/conflict-of-interest property and narrow question. There is no automatic third reviewer, Coordinator tie-break or indefinite chain. Missing fresh-review capability preserves the affected blocker without another stage. Apply Author's whole-solution reset to cumulative corrections before another local patch.

## Refresh check

For substantive implementation, refresh on entry/re-entry/context replacement, before first material commitment, between slices, after failed hypotheses/material failures/findings/accepted corrections, before material shape changes and readiness/handoff/completion claims. For review, refresh on entry/re-entry/context replacement, before review, after candidate/evidence/currentness/focused corrections and before verdict/handoff.

Reopen exact candidate/control SHA and re-ground outcome/non-goals, owner/reuse seam and governing plan; changed shape and smaller delete, reuse or reframe route; missing oracle/evidence and NOT_RUN/SKIPPED/MISSING_CAPABILITY/UNKNOWN; and scope/authority/boundaries for next commitment. This is reasoning, not a new gate, checklist, timer or authority.

## Deterministic and execution truth

Executable contracts/CI own deterministic gates. Structural/complexity evidence is diagnostic absent an exact enforced invariant; retain qualification distinctions. No deterministic trajectory monitor exists: plan/refresh/review requirements do not prove conformance. Until natural adoption evidence, guidance plus readiness/review detects deviations; enforcement is separate design. Existing owner retains cumulative audit scheduling; Code Audit names concrete oversized-staging defect/minimum clearing condition without silently redesigning the package.
