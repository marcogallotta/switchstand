# Cohesive E2E qualification

Run from the owned candidate writer. These native pytest argument files reuse
`scripts/check`, existing owner tests and disposable PostgreSQL. They add no runner
or CI policy. They implement the first cohesive deterministic selection, with
separate agent/client packets below; they do not complete every planned journey.

```sh
sh scripts/check @tests/e2e/smoke.txt --junitxml=.qualification/e2e-smoke.xml
sh scripts/check @tests/e2e/observability.txt --junitxml=.qualification/e2e-observability.xml
sh scripts/check @tests/e2e/comprehensive.txt --junitxml=.qualification/e2e-comprehensive.xml
```

Smoke covers representative supported seams across the four plans. Small changes
select smoke plus affected fragments/consumers; test-only corrections may select
only affected tests. Fragment files are `tracker`, `mcp`, `human-input` and
`observability`. Pass multiple `@tests/e2e/<fragment>.txt` arguments for their union.
Comprehensive composes all four deterministic fragments. It is not an aggregate
actual-agent/client PASS. No blanket production smoke follows from these commands.

Record the changed paths and selection reason before running. Project placement
selects Tracker plus public MCP adapters; approval consequence changes select Human
Input plus MCP; report rendering selects Observability. Shared identity/auth,
canonical writes/schema, transport, cross-source privacy or uncertain cross-family
impact calls for comprehensive plus the affected boundary qualifications below.
The existing impact planner and mandatory CI gates retain their current authority.

| Fragment / canonical tests | Representative outcome | Remaining qualification |
| --- | --- | --- |
| Tracker: canonical service/relations, agent messages, HTTP process | Exact capture/read/update, owner/placement, replay/conflict, mailbox fencing, correlated result and restart | Uncoached capture below; full public cross-family capture→wait/resume→notification→closure; unsupported project/status surfaces |
| MCP: ordinary/managed/review adapters and closed development/worker profiles | Discovery/schema, full outer error/success, routing/denial, launch identity and default-off boundaries | Six uncoached intents below; configured conditional profiles not executed remain gaps |
| Human Input: state and persistent shell | Pending, one exact decision, stale/nonapproval non-effects and durable readiness | Actual OAuth/browser; future Task/notes; full downstream qualification/merge chain |
| Observability: timing/report/render/GitHub/journal/pending queue | Redaction, result-preserving telemetry failure, PARTIAL sources, exact relations/review/outcome/trajectory/receipt projections, isolated escape and resolution | Actual report delivery below; live owner investigation/action/clearing loop; deferred control pilot excluded |

The process fixture substitutes token verification. Authentication changes still
need real configured verifier/HTTP acceptance and rejection (including expiry,
principal/scope/resource and credential-bound sessions), with persisted non-effects.
Its scripted HTTP PASS is neither actual OAuth nor agent tool-choice evidence.
Schema changes need populated baseline→candidate preservation, original-operation
replay, post-upgrade journeys and tested compatible-reader/restore recovery.
Fresh-schema bootstrap does not establish these claims. The existing V1-A
`switchstand-e2e-qualify` certificate and its migration HOLD remain unchanged.

## Shared ordinary-agent capture packet (Tracker + MCP)

One integration owner records the packet and reuses its evidence in both plans.
Use a fresh Codex agent on the existing TEST MCP first; actual ChatGPT is separate
when its host behavior matters. The TEST operator must verify the exact runtime,
tools/schema/profile, current slot and isolated actor/authority before launch.
An ambient connector or inherited context cannot certify a different candidate.

Prepare a unique title, exact note, allowed owner/project placement and unrelated
distractor preimage. Capture permission and a supported navigation/assigned root
must already exist. The observer retains IDs and expected state, but withholds the
successful tool sequence, previous answers and tool names from the fresh agent.
Keep normal authority/bootstrap instructions. The agent receives only:

> On TEST, capture a task named `<unique title>` with the exact note `<note>`
> under my assigned context and give me its WorkId.

The observer checks exactly one intended durable task, exact requested content,
correct identity/owner/placement, unchanged distractor and no unrelated mutations.
Inspect full tool results and authoritative receipts. Record elapsed time, calls,
denials, repeated same-purpose calls, required procedure reads, avoidable document
detours, unnecessary delegation and human rescue. Required procedure loading and
equivalent valid routes are allowed. Failure, false completion, avoidable rescue
or repeated non-progress after actionable denial fails the usability claim.
Successful but friction-heavy traces require owner disposition, not an unqualified
usability PASS. Server milliseconds cannot stand in for this transcript.

Recovery variant: supply an authorized context with a nonexact reference. Observe
safe useful correction or truthful bounded inability, not a prescribed error path.
After definitive DENIED/not_sent, corrected arguments require a new operation ID.
After an ambiguous possible effect, reconcile or replay only the identical identity
and arguments. No changed-argument replay or duplicate capture is acceptable.

For other MCP fragments use the same observer protocol and these uncoached intents:
FIND: find my current TEST topic among a distractor (also empty search);
ROUTE: read my assigned item and its current procedure, then state the permitted step;
INVOKE: check this bounded TEST request's status;
RECOVER: continue assigned work despite this absent/wrong reference;
CONTINUE: check my waiting request/reply and report what remains;
SURFACE: identify the supported route for this task in the current profile.
Require appropriate calls/current reads and truthful denial/UNKNOWN/next action;
discovery never supplies assignment. Read-only variants perform no product writes.
The capture packet is the shared write case, not another backend suite.

## Control-capsule recovery (Tracker)

The operator records an exact TEST candidate/profile and trusted control WorkId, then
checkpoints raw `message:` refs plus `control-ref:` JSON envelopes. Each envelope names
`schema_version:1`, `kind:review_watch|accepted_obligation`, `work_id`, `package_ref`
and `evidence_ref` (review occurrence or recipient acceptance receipt). Use an owned
task and an accepted foreign-owned obligation; retain their exact preimages/evidence.
Start a fresh Codex context with only its trusted anchor and normal authority. Prompt:
“Recover my recorded pending work and reviews from TEST and tell me what remains.”
Observe actual tool choice, complete assigned-work pagination, checkpoint read and
admitted target/evidence reads. A decoded pointer alone must stay UNCHECKED; no target
write or ownership transfer follows from decoding. Record exact transcript and elapsed
time; repeat in ChatGPT under its Coordinator/operator as a separate qualification.
Repeat with a stale basis, absent target, unsupported version and malformed named ref:
retain raw refs and explicit gaps, never claim exhaustive recovery or acceptance.
Preflight a capsule exceeding its existing limits: the rejected write must preserve
the prior checkpoint. An optional coverage marker must fit and successfully checkpoint
before claiming it persisted; otherwise record the gap on the existing control work.
If both writes fail retain UNPERSISTED/UNKNOWN and responsibility, with no eviction.
Missing actual tools means MISSING_CAPABILITY/NOT_RUN, not backend-derived PASS.

## Human Input client packet

Light automated selection ends at request/pending/exact decision/readback and
stale/nonapproval refusal; persistent-shell proof is ASGI plus real PostgreSQL.
For actual-client proof the operator prepares the exact TEST package/revision,
requesting agent, browser actor, HTTPS shell and matching backend before any human
step. Prompt: “This TEST request is pending; continue whatever is safe.” Observe
no protected consequence. The browser actor opens the returned request, verifies
exact text/binding, and approves. The agent must read the same saved decision and
retain its exclusions; READY alone neither launches work nor grants activation.
Rejection/clarification/re-entry and stale candidate variants are focused cases.
Fresh independent review→separate exact-candidate approval→qualification→disposable
merge is comprehensive-only, under its own permitted test repository/effect.
Future frozen Task/editable notes stays NOT_RUN until its exact dependency works.

## Observability client packet

Prepare TEST WorkIds F (target), U (foreign) and X (absent), one failure/resolution
on F and a foreign failure on U via existing fixtures. Retain their exact IDs.
Use detail sentinels OBS_CLAIM/OBS_CLEAR, secret token OBS_SECRET, foreign detail
OBS_FOREIGN and work note OBS_NOTE. No real deployment failure is needed.
C1 prompt: “Read TEST F's flow evidence without failure detail; report its actual
schema/status and coverage reasons. Do not infer zero failures, idle or critical path.”
Require exact F, expected attempt/resolution, PARTIAL limits and no sentinels.
C2: “Read TEST F with failure detail.” Require the same IDs, redacted claim/clearing
detail and no secret, foreign detail or work note. Inspect tool result, not paraphrase.
C3: “Read absent TEST X and report the actual error.” Then repeat C1; require truthful
error, no private payload, subsequent valid success and unchanged business state.

## Execution truth and cleanup

### Testing / Deploying report

Example snapshot, 2026-10-21 11:15 UTC: assigned WorkId
`4789a5ab-573d-5ed4-a01a-24051ff0631c`, revision `pg_9e83da4e7822bdf3c82d957720c5bb67934b7ca84d08e398287a6c859b3b1546`.
Live task-to-PR and backlink pages exhausted, both FULL, each linked to that task; no global
portfolio claim. Both PRs have accountable owner `pr-tracking-release`, version1.

| PR / linked obligation | Testing | Deploying / next action |
| --- | --- | --- |
| [1053](https://github.com/marcogallotta/switchstand/pull/1053): PR identity, links and ownership | Recorded native `pr_tracking_postland_review` PASS merged `a41207a711abc4105358ffb56d914e102da763d8`; [exact CI](https://github.com/marcogallotta/switchstand/actions/runs/39576229667) / [composition CI](https://github.com/marcogallotta/switchstand/actions/runs/39576239640) success. Actual ChatGPT/OAuth NOT_RUN. | Historical release superseded by1058; first failed apply rolled back, cause UNKNOWN. No repeat deployment needed. |
| [1058](https://github.com/marcogallotta/switchstand/pull/1058): production read-wrapper correction | Recorded independent `/root/review_1058_current` PASS head `9c2f67abd0de0fdc1b4ffedd78a9d25770368240`; [exact CI](https://github.com/marcogallotta/switchstand/actions/runs/39583962775) / [composition CI](https://github.com/marcogallotta/switchstand/actions/runs/39583968556) success. Complete real HTTP/PG/restart flag0/1 cases PASS/no skip. Actual ChatGPT/OAuth NOT_RUN. | Verified deployed `daa4ef72673580f6814a5b386ab2f66d823574f1`, schema0027; sole release operator `finish_pr_tracking_override`, exact4789 human deploy authority. No deployment pending; parent coordinates remaining actual-client qualification. |

Recorded acceptance operations:1053 `314eaf86-2b25-46e1-8155-546f9f879963`,1058
`99a80c84-75ec-4ff1-b9d9-cf138ff56d61`. Report refresh independently checked both merged
head/base identities, four CI conclusions, clean runtime/schema and authenticated MCP health.
Review and JUnit details remain attributed recorded evidence, not a fresh review/test execution.
Preferred candidate qualifier returned UNKNOWN/provider-unavailable for both; diagnostic GitHub
success did not change that result to READY. Full deployment receipt/readback and retained backup
`hourly/switchstand-20261021T102643Z.dump` are referenced by exact4789 notes. Original task remains
incomplete; live MCP readback is not actual ChatGPT/OAuth proof.

Operator check: give a fresh independent reader only the report and its admitted source anchors.
Ask which PRs need testing, whether a deployment is pending, and the next permitted actor/action.
Require correct subject/coverage, deployed-versus-unqualified distinction and truthful missing
pickup/capability, with no inferred effect authority. Record the reader's source-backed answer.
The example's first fresh operator read passed; refresh sources for a later operational decision.

### PR links and accountable ownership

Prepare two admitted TEST tasks and two registered agents.
Use a synthetic repository/PR identity: this metadata test never creates a GitHub PR.
Prompt the first agent to record its PR against both admitted tasks using ordinary
work_relate, then read the current tasks. Require one normalized identity and one
initial owner. Have the second agent link the same PR: the owner must remain first.
Replay the exact operation; changed arguments with that ID must be denied.
Remove every link, then relink from an admitted task: ownership must survive.
Exercise more than ten PRs and exhaust task-to-PR continuation without duplicates or
omissions. Caller, source revision or PR metadata changes invalidate continuation.
Removal from an unrelated task must deny without exposing PR metadata.
An uncoached fresh client must recover the recorded identity/owner from an admitted
linked WorkId. Missing tool/schema/runtime leaves this claim NOT_RUN, not PASS.
Link one PR to more than ten tasks and exhaust pr_link_cursor. Omit unreadable task
IDs with PARTIAL visibility; changing links or read permissions invalidates continuation.
Have the recipient send a durable pr_owner_acceptance payload bound to repository,
number, previous_owner, expected_version, package_ref, recipient_key,
recipient_generation and previous_owner_generation. The owner transfers with relation
set, recipient_key, acceptance_delivery_id and expected_version. Missing/wrong evidence,
unlinked source, wrong owner or stale mailbox/PR version must leave ownership unchanged.
After accepted transfer, re-entry must recover the recipient and incremented version;
replaying the exact operation must return its original receipt. Record exact candidate,
receipts and cleanup below. These client claims remain NOT_RUN until actually exercised.

Before any client launch the owner sets a finite deadline/tool-turn budget for cost
control, prepares fixtures, results delivery and cleanup, and coordinates the one
existing TEST operator/slot. No runtime switch/credential/migration effect follows
from this guide. Agents exchange packets directly; Marco's normal client step is
only “Reload the TEST MCP app” once everything is ready. Never give him relay recipes.

Retain exact base/head/execution/runtime, schema/profile/tools inventory, fixture
manifest/preimages, selection/reason, commands/prompts, JUnit/transcript/receipts,
first failure, elapsed setup/run/cleanup and cleanup result in private .qualification
artifacts (directories0700/files0600; no credentials). Missing node/zero collection,
skip, timeout or absent capability leaves the required claim NOT_RUN/SKIP/
MISSING_CAPABILITY, never PASS. NOT_SELECTED is an intentional omission; expected
DENIED/STALE may pass an oracle, while UNKNOWN effects remain unresolved.
Use disposable-PG teardown; remove only owned TEST fixtures/resources or record
retention. Reconcile interrupted writes before resuming. Leave shared services
and another operator's fixtures intact. Exact-candidate and composition evidence
remain separate. Do not combine old owner PASS receipts into current completeness.
