# Human Review baseline and conformance — Implementation Specification

Status: independently reviewed implementation specification. No activation/deployment authority.

Design: WorkId `5998b00d-2777-5eee-bd4a-8197bdfc7209`, approved 2026-10-16 at
`pg_45e71752ba06dcdc34a57d8b0812f04cfd0c83d12d66eb54383f507b54181b92` after DESIGN PASS
`efef96e2-e64a-597e-9d06-57b540ebbc2b`. Immutable package base:
`d117c0c923910361f2cbe1c3937aa2b4931c0eda`. The only relevant drift from the design's older
`2662f83b5682a678f310192a2770addde6c98c51` basis is the code-review danger hotlist; it applies
without changing this design.

## Outcome and invariants

Evolve the existing Human Review, typed review, and implementation-request owners so:

1. Approval stores an immutable baseline containing human sources, the approved four-line opening,
   protected meaning, non-goals, decision context, and explicitly named continuation effects.
2. Before dispatch, the owner records an exact package/baseline comparison; independent review
   explicitly validates it; admission accepts only `BASELINE_PRESERVED`.
3. Final exact-candidate review confirms baseline conformance and absence of a new human-owned
   choice. A conforming build gets no second Human Review solely because it is complete.
4. Approval clears Human Review for effects named by the exact handoff. Implementation, landing,
   or activation continues only where current assignment/grant separately authorizes that effect.

History is append-only. Removal or weakening of a protected requirement is material and cannot be
implemented before approval. Ordinary realization that preserves outcome, experience, trust/data
boundaries, burden, risk, and protected effects is not material.

Owner comparison and reviewer assessment are separate attributable facts. Author notes, green
tests, or unqualified PASS are insufficient. Baseline, candidate WorkId/revision, review occurrence,
assessment, and verdict digest bind together. Replay remains exact.

`UNKNOWN` cannot dispatch or establish conformance, but blocks only that readiness claim. Minimum
evidence, correction, investigation, unaffected branches, and authorized option-preserving work
continue; no HOLD, WAITING, priority, or lifecycle mutation is created.

No new table, service, process, store, workflow engine, state machine, public tool, grant operation,
runtime flag, or runtime-config crossing. Reuse `HumanReviewState`, `ReviewOccurrenceState`, and
`ImplementationRequestState`.

## Human Review presentation

Render this opening first and in order:

```text
WHAT YOU ASKED — <intended or approved outcome in human framing>
WHAT THIS DOES — <actual ordinary-use change>
WHAT THIS DOES NOT DO / WHAT CHANGES FOR YOU — <exclusions, cuts, burden, limits, consequences>
SIZE — <decision-useful production/support/package size or operating burden, with uncertainty>
```

Only then add `RECOMMENDATION`/`DECISION` when needed. No decision-critical fact appears only later.
The actual opening is reviewed inside the existing review; findings changing its decision surface
receive focused rereview, not a new stage.

## Data contracts

### Approved baseline

Extend `HumanReviewConsequence` with optional closed `baseline`; optionality keeps stored V1 rows
readable. New proposals use V2:

```text
HumanReviewBaselineV2
  schema_version: "2"
  human_source_refs: tuple[str, 1..32]
  opening: {what_you_asked, what_this_does,
            what_this_does_not_or_changes_for_you, size}
  approved_meaning: tuple[str, 1..32]
  protected_requirements: tuple[str, 1..64]
  approved_non_goals: tuple[str, 0..32]
  decision_context:
    size_or_burden: tuple[str, 1..16]
    scale_assumptions: tuple[str, 0..16]
    material_risks_and_boundaries: tuple[str, 0..32]
  continuation_effects: tuple["IMPLEMENT" | "LAND" | "ACTIVATE", 1..3]
  supersedes_baseline_id: UUID | null
```

Strings are nonblank, at most 1,024 characters; tuples are unique. Existing
`package_work_id/revision`, implementation scope/target, and excluded effects remain. V2
`approval_effect` is `CLEAR_HUMAN_REVIEW_FOR_NAMED_CONTINUATION`; legacy
`READY_FOR_IMPLEMENTATION_ONLY` remains readable. LAND requires a repository landing target;
ACTIVATE cannot also be excluded; no effect may be both continued and excluded.

The deterministic `consequence_id` is `baseline_id`. The existing row supplies exact occurrence,
package revision, decision, and `decided_at`; APPROVED creates a baseline. Supersession must resolve
inside the proposal transaction to an APPROVED V2 row for the same WorkId. An existing approved V2
baseline requires exact `supersedes_baseline_id`; concurrent proposals converge or conflict.

V1 rows retain same-revision behavior only. They never become cross-revision baselines; no backfill
invents human meaning. Malformed V2 is UNKNOWN, not V1.

### Owner check and reviewer assessment

Add optional closed `baseline_check` to `ReviewRequest` and `ReviewBasis`:

```text
BaselineCheck
  baseline_id: UUID
  phase: PRE_IMPLEMENTATION | FINAL_IMPLEMENTATION
  owner_disposition: BASELINE_PRESERVED | MATERIAL_DEVIATION | UNKNOWN
  comparison: bounded nonblank str
  evidence_refs: tuple[str, 1..32]
```

The service requires an APPROVED V2 baseline for the subject WorkId and copies the check into the
basis, stable identity, brief, digest, and focused-review comparison. PRE evidence identifies the
exact plan/spec; FINAL evidence identifies the immutable candidate/qualification.

Add optional closed `baseline_assessment` to `ReviewSubmit`, `ReviewOutcome`, and `ReviewResult`:

```text
BaselineAssessment
  baseline_id: UUID
  phase: PRE_IMPLEMENTATION | FINAL_IMPLEMENTATION
  disposition: BASELINE_PRESERVED | AUTHOR_CORRECTION_REQUIRED |
               HUMAN_REVIEW_REQUIRED | CONFORMS_TO_APPROVED_BASELINE |
               MATERIAL_DEVIATION | NEW_HUMAN_CHOICE | UNKNOWN
  evidence_refs: tuple[str, 1..32]
```

No check means no assessment. A checked terminal review requires matching baseline/phase. PRE PASS
requires BASELINE_PRESERVED; FINAL PASS requires CONFORMS_TO_APPROVED_BASELINE. Correction,
deviation, new choice, or HR required uses FINDINGS; UNKNOWN uses BLOCKED and identifies minimum
missing evidence. Assessment is in verdict digest/envelope validation. Legacy outcomes remain
ordinary review evidence but cannot satisfy a baseline gate.

Add internal projection:

```text
BaselineReviewProof
  status: PASS | NOT_PASS | UNKNOWN
  baseline_id, phase, review_id, verdict_digest, reason
```

`baseline_pass_status_in_transaction` returns PASS only for one current exact PRE PASS with
BASELINE_PRESERVED; no match is NOT_PASS; malformed/duplicate/contradictory/unreadable state is
UNKNOWN. Keep general `pass_status_in_transaction` unchanged.

## Persistence and presentation

Use existing consequence JSONB and durable review envelopes; no migration. `human_review_record`
parses V1/V2. Proposal supersession validation uses the current transaction/locks. Later candidates
cite immutable baseline ID rather than mutate approval.

`human_review_shell.py` renders the V2 four lines first, then protected requirements, non-goals,
decision context, continuation, scope/target, and exclusions. Preserve HTML escaping, auth/origin,
form bounds, decision buttons, digest/currentness. `human_review_propose.py` retains its bounded
mode-0600 no-follow input and cannot decide, dispatch, land, or activate. No public HR tool.

## Admission algorithm

In `ImplementationRequestState.request`:

1. Preserve exact operation replay checks.
2. Lock/verify current active package WorkId and revision.
3. Read exact `BaselineReviewProof`: NOT_PASS -> DENIED `baseline_review_not_passed`; UNKNOWN ->
   UNKNOWN `baseline_review_unavailable`; neither creates a request or changes work state.
4. Under the same transaction require exactly one valid APPROVED V2 baseline matching proof and
   package WorkId. Missing/nonapproved/mismatch is DENIED; corrupt/ambiguous is UNKNOWN.
5. Preserve current principal/send-grant checks.
6. Bind baseline ID/digest plus review ID/verdict digest in `authorization_ref`, reconcile the
   exact stable request, and admit atomically via `TaskRunState`.

Legacy V1 keeps the current same-revision approval + ordinary PASS path only. Public
`implementation_request` arguments stay unchanged; baseline/review identities are server-derived.

V2 result contract adds baseline ID/digest, PRE review ID/digest and `continuation_effects`.
Derive milestones only as IMPLEMENT->IMPLEMENTED, LAND->MERGED, ACTIVATE->ACTIVATED. Never emit
MERGED/ACTIVATED merely as vocabulary; activation-excluded handoffs omit ACTIVATED. Milestones
describe route, not effect authority or proof.

## Final conformance and deviation

FINAL review compares exact candidate with baseline: protected requirements and non-goals remain;
no unresolved deviation; no new/changed human-owned outcome, product experience, architecture,
trust/data boundary, operating/support/cost burden, material risk, or protected effect. PASS +
CONFORMS_TO_APPROVED_BASELINE continues through current review/CI/composition/landing gates without
repeat HR. MATERIAL_DEVIATION/NEW_HUMAN_CHOICE routes only the affected choice before commitment.

For deviation, reviewer starts from baseline/human source and asks: weakening? necessary or smaller
preserving route? human-owned versus realization? mature enough to state outcome, exclusions,
rough burden, strongest alternative, recommendation? Author correction uses focused rereview.
Only a surviving mature human choice reaches HR before implementation. New approval creates an
append-only superseding baseline; never retroactive approval.

No automated landing gate is added. Existing code-quality/landing procedure consumes typed final
review. Landing and activation evidence/authority remain separate.

## File plan and acceptance

Production: `human_reviews.py` V2 model/state/reader; `reviews.py` typed check/assessment/proof;
`implementation_requests.py` V2 admission/V1 compatibility/derived milestones;
`human_review_shell.py` opening; `chatgpt_mcp.py` existing review schemas only. Update
`human_review_propose.py` only if closed-model parsing requires it.

Tests cover: V2 approval/supersession/concurrency/corruption/V1; shell ordering/escaping and retained
guards; check identity/focused transition, assessment/digest/phase combinations, legacy review;
MCP optional fields/no new tool; Postgres admission/replay/mismatch/deviation/UNKNOWN/no lifecycle
mutation/derived milestones/V1; owning documentation. Existing flow reporting remains readable;
add baseline fields only if needed for truthful current state.

Required scenarios: conforming plan/candidate dispatches/lands without second HR; removal/deferral
cannot dispatch; internal mechanism change conforms; larger burden tests smaller route; new trust or
operating boundary is NEW_HUMAN_CHOICE; churn alone does not trigger HR; compressed opening is
corrected in existing review; activation-excluded contract omits ACTIVATED; expressly approved and
separately authorized activation may continue; UNKNOWN creates no request/hold and unaffected work
continues.

## Procedure changes

- `AGENTS.md`: replace unconditional post-approval stop with exact named continuation plus separate
  effect authority.
- `docs/human-review.md`: baseline, four-line opening, focused deviation, no redundant HR,
  continuation.
- `docs/code-quality.md`: owner PRE comparison and reviewer FINAL confirmation; scoped UNKNOWN;
  separate effects.
- `docs/architecture.md`: V2 ownership/binding/V1 compatibility.
- `docs/human-interaction.md`: link the HR-specific opening without replacing general formats.

The old exact `Human Review approved. Dispatch ready: <WorkId>.` is optional only when it truthfully
describes a separate handoff; it cannot force a needless return to Marco.

## Mandatory package block

- Identity: WorkId above; approved revision above; base above.
- Integration owner: current Work owner `agent:human-review-redesign-20261016` unless exact current
  work records attributable transfer.
- Original upper forecasts: production 400, support 600. Margins: 100/150/250. Hard caps:
  production 500, support 750, total 1250. Binary allowance 0 files/0 bytes.
- New tables/tools/services/processes/runtime flags/grant operations/persistent stores/workflow
  engines/state machines/runtime-config crossings: zero each.
- One implementation PR: state/review/admission plus evidence/docs is one useful vertical behavior;
  splitting leaves an unenforced claim or an unproducible proof. Smaller docs-only route fails the
  approved durable identity/admission outcome.
- Earliest discriminator: Postgres-backed approved V2 + exact PRE PASS dispatches once; ordinary
  PASS, mismatched baseline, or UNKNOWN creates zero requests.

Package paths are: `AGENTS.md`; the five docs named above plus this spec; production files named in
File plan; `tests/test_{chatgpt_mcp,documentation,flow_report,human_review_migration,
human_review_propose,human_review_shell,human_reviews,implementation_requests,review_mcp,reviews}.py`.
Support patterns are `AGENTS.md`, `docs/*`, `tests/*`; named `src/switchstand/*` files are production.
Any other changed path is UNKNOWN/replan.

Run on final combined head:

```bash
python scripts/package-size.py \
  --base d117c0c923910361f2cbe1c3937aa2b4931c0eda --head HEAD \
  --package AGENTS.md --package docs/architecture.md --package docs/code-quality.md \
  --package docs/human-interaction.md --package docs/human-review.md \
  --package docs/human-review-redesign-implementation-spec.md \
  --package src/switchstand/chatgpt_mcp.py --package src/switchstand/human_review_propose.py \
  --package src/switchstand/human_review_shell.py --package src/switchstand/human_reviews.py \
  --package src/switchstand/implementation_requests.py --package src/switchstand/reviews.py \
  --package tests/test_chatgpt_mcp.py --package tests/test_documentation.py \
  --package tests/test_flow_report.py --package tests/test_human_review_migration.py \
  --package tests/test_human_review_propose.py --package tests/test_human_review_shell.py \
  --package tests/test_human_reviews.py --package tests/test_implementation_requests.py \
  --package tests/test_review_mcp.py --package tests/test_reviews.py \
  --support AGENTS.md --support 'docs/*' --support 'tests/*' \
  --production-forecast 400 --support-forecast 600 \
  --production-cap 500 --support-cap 750 --total-cap 1250
```

Stop/replan before any zero-count surface, free-form conformance parsing, old approval against later
candidate without exact bindings, Human Review alone granting protected effect, history rewrite,
uncovered/binary change, or cap breach. Before landing provide exact base/head size report, affected
tests, no-migration proof, final assessment/digest, documentation dispositions, independent PASS,
and current landing/CI/composition evidence. Do not activate.
