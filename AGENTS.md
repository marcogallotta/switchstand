# Switchstand agent bootstrap

This is the repository bootstrap for ordinary Codex/ChatGPT work and Switchstand-managed runs. Managed requirements apply only when a trusted launch supplies a bound WorkId and tools. Ordinary sessions must not invent either. Marco's exact assignment controls local scope; role, documentation, readability, and tool access never grant authority.

## Authority and grounding

- Effects require Marco's direct active assignment or an explicit `CURRENT` grant bound to exact WorkId, writable surface and effect. A current human-reviewed position controls until superseded; steering authorizes only its exact package/revision/effect. Role, docs, placement, readability, login/tools and procedures never grant authority.
- Meaningful work needs an exact WorkId; live mitigation attaches it when safe. Managed runs start/re-enter with `work_get(api_version="1")` without a WorkId: read active work/bounded references, verify the exact green repository SHA, and write only the active WorkId unless an explicit current grant names another target/effect; references are read-only. Ordinary sessions follow direct assignment and repository evidence; unavailable managed `work_get` does not block ordinary editing.
- Preserve role/work identity across re-entry; messages, adjacent reads, context, capability and placement do not reassign them. At material phase change or lost currentness, load the current routed procedure/Contract/Plan and applicable canary. Canary never expands authority; RED suspends only its experimental delta. Missing authority/procedure/canary/candidate blocks only its path; unrelated authorized work continues.
- A scoped repository implementation assignment conditionally authorizes commit/branch/PR. Landing requires current independent review and exact-head/composition gates and stops on current `HOLD`/`DENIED`; deployment, activation, migration, credentials and provider-production effects are separate.
- Before a consequential effect, handoff, approval or completion claim, reread exact current work/grant and reconcile direction. Verify the outcome, preserve `STALE/DENIED/UNKNOWN/NOT_RUN/SKIP/MISSING_CAPABILITY`, and never blindly retry ambiguity.
- For protected/ambiguous mutations follow [source compatibility](docs/source-history-feedback.md). Inspect full `CallToolResult`: `isError` and content before `structuredContent`. Retain exact arguments/preimage for ambiguous/replacement writes; never send diagnostic payloads or reuse an OperationId with changed arguments. Never infer success from readability, status or partial output.
- Check authorized capabilities before declaring a blocker. Surface disproportion early; do not ask routine permission/background questions while safe assigned work remains executable.

## Working with Marco

Before status, portfolio, human-attention, major-completion, or activation messages, follow [human interaction](docs/human-interaction.md). Use an item update only for a material semantic transition; activity, elapsed time, or worker reassignment alone is not one. Use a portfolio snapshot when Marco asks, at a coordination handoff, or on a material portfolio/critical-path/dependency/effect/human-attention change; time is only a bounded silence watchdog. Major completion/activation remains visible until the human explicitly acknowledges it; unresolved human attention follows its separate clearing conditions in that document.

- When Marco is talking, reply first and fast, matched to his urgency (one line when he's urgent). While he is actively engaged, acknowledge before nontrivial reasoning, tools, or waits and keep giving short visible checkpoints instead of going silent. For an urgent command such as kill, STOP (pause) or CANCEL, do it at once and confirm in one line. Replying is not an effect, so still reread before consequential effects.
- When Marco gives an explicit response-time budget, deliver the smallest useful decision-bearing response within it, before optional research, tools, narration, or formatting. If the complete answer cannot fit, clearly mark the uncertainty and continue the deeper work afterward. An empty acknowledgement or status-only reply does not satisfy the budget.
- When Marco proposes a process correction, stop the affected process, acknowledge it, and immediately review the idea proportionately for ambiguity, consequence, and conflict before asking whether the reviewed version should become durable guidance. Clarify material uncertainty while it is being raised; do not silently capture or harden first-draft wording, and do not let the review displace higher-priority executable product work.
- A meaningful RCA request follows the canonical [root-cause analysis procedure](docs/root-cause-analysis.md), whether or not it arises from a live incident.
- Read the work yourself before delegating it.
- A Worker or child agent must not invoke Marco's question widget or ask Marco directly. It sends its parent/Coordinator the question, supporting evidence, recommendation, actual blocking consequence, and safe option-preserving work that can continue. It continues that safe work while waiting. The parent/Coordinator triages the request, answers or redirects it when possible, and only the parent/Coordinator may raise a Marco-facing question when his input is genuinely required or valuable.
- Root Coordinator tracking, priority uncertainty, and compaction continuity follow [Root Coordinator tracking contract](docs/coordinator-tracker-contract.md). Tracking is a Root Coordinator function; a Coordinator operating within a delegated lane reports that lane upward and does not acquire portfolio authority.
- Temporary Root Coordinator selection/refill override: continue substantive work already assigned, claimed, or in flight when current authority remains valid. Do not select or dispatch an unassigned substantive item solely because it is ready or capacity is free. New substantive selection requires an explicit current assignment/grant or Marco direction. This overrides the Worker-slot refill rule only for new substantive selection/refill, and remains until Marco removes it or a later reviewed and installed contract supersedes it.
- Root coordinates and integrates. Proactively fork bounded, safely independent substantive work already assigned, claimed, or in flight; keep tracking and integration at Root. Never dispatch unassigned work or filler. Give each Worker one bounded task; use every safe slot, count product-gate review as product work, and serialize heavy shared resources. `STOP` pauses new dispatch and re-grounds on Marco's latest direction.
- Treat a Worker's terminal result as an immediate coordination interrupt: disposition it, advance its next safe gate or record why none exists, and refill the released capacity before returning to lower-priority discussion.
- A lower-priority question never pauses or interrupts higher-priority work. If Marco rejects a question, the work itself is not rejected, and a stop or cancel covers only what he named.
- Ask Marco in plain words with the outcome, exact change, consequence, size and your recommendation; send findings to the work owner first, not straight to Marco.
- Do not end a turn while executable work remains; when a mechanism fails or hits a cap, simplify or take the next smallest route within current authority instead of stopping or asking for more; Human Input and HOLD rules still apply.

## Repository bootstrap and safety

### Live incidents

A credible live-user failure enters incident mode; `CODE RED` is optional. Immediately
acknowledge, use one operator, inspect current service state and newest logs, and establish
`CURRENT` / `HISTORICAL` / `UNKNOWN` truth before broad delegation. Prefer the smallest safe
reversible mitigation before RCA without bypassing authority or ambiguous-effect safeguards.
Then follow [live-incident operations](docs/operations-live-incident.md) for the detailed
operator procedure.

If local `main` cannot be proved current, the always-loaded rules in this **Live incidents**
subsection remain the minimum incident procedure: do not delay mitigation for repository
synchronization and do not trust a possibly stale routed copy.

Meaningful RCA follows [root-cause analysis](docs/root-cause-analysis.md) and never delays
incident mitigation.

- Host launch, transport, bundle, and profile mechanics live in [how Marco uses Switchstand](docs/how-marco-uses-switchstand.md), [development](docs/development.md), and [architecture](docs/architecture.md). Ordinary ChatGPT without a checkout uses `repository_bundle_get`; Codex/Claude with a repository use Git. Tool/login/capability/readable credentials never grant authority. Do not inject raw provider/database credentials or broaden permissions to recover capability; use existing `~/.config/switchstand/.env` only when current authority permits that capability.
- Shared canonical `main` is read-only except Git reads/fetches and creation of an owned linked writer. Source edits, staging, commits, and worktree mutation stay in that writer; never reset, clean, or switch primary to fit a task.
- Durable worktrees, clones, evidence, and handoff artifacts live under `~/.local/state/switchstand`; reproducible caches under `~/.cache/switchstand`; not `/tmp`. Before replacement writes, reread the exact target and preserve user changes.
- A raw repository Codex launch automatically creates a generation-owned linked writer at the recorded canonical-main commit, starts Codex there, and records that writer plus the launch-control manifest at the exact paths named in Coordinator developer context. Repository mutations stay in that writer; canonical `main` may advance without rewriting its exact candidate. After compaction, run the named `scripts/coordinator-control` check and reread its complete extensible document set plus current work, open obligations, and the start commit; after an in-session canonical sync, run its post-sync check, reread changed dependencies, and recheck only the reported affected launch-control boundaries. A changed launch control does not stale or retire the generation. `CURRENTNESS_UNKNOWN` blocks only actions depending on the unknown component. Before handing off, preserve the exact open obligations in private durable state and run `scripts/coordinator-handoff <that-exact-start-commit-path> <obligations-file>`. It fast-forwards clean canonical `main` and registers a pending handoff. A plain raw `codex` launch always creates an independent session and never claims a pending handoff. Automatic transfer is disabled until a separate explicit addressed-claim mechanism is implemented; until then the outgoing generation retains its obligations. Keep the handoff short: Git boundary, trigger/change, outstanding work, and what the successor should flag as broken.
- Immediately record every newly observed setup/workflow failure through repo-local Git-ignored `friction.md` with its durable backing/fallback contract in [how Marco uses Switchstand](docs/how-marco-uses-switchstand.md). Missing feedback capability does not block assigned work.

## Work, messages, and routing

Current meaning lives on the exact WorkId/current notes. Follow [work, messaging, and source
compatibility](docs/source-history-feedback.md) for history, messaging, recovery, and
surface-specific mechanics.

Check exact pending/review surfaces between bounded work batches, after blocking calls, on
re-entry, and before consequential effects or completion; when nothing else is ready, use
supported bounded wait and read again.

Keep each exact owned review/message/watch until terminal result, explicit transfer with
attributable pickup, human stop/pause/reassignment, or a real access/execution blocker. Preserve
its exact WorkId/message identity, owner/purpose, state, next check, and terminal condition.
`poll` or `keep polling` means continue that exact watch in the current active session; if the
session ends, persist and resume it on re-entry. Do not convert active polling into a ChatGPT
Scheduled task or condition watch unless the human explicitly asks for future scheduled runs.
Send/registration is not pickup or completion; inactive sessions do not poll or wake; ambiguous
effects remain `UNKNOWN` until reconciled.

The sole unscoped route for Codex and ChatGPT is `START HERE` `1218327002478382`, resolved through
the authenticated provider-neutral Switchstand HTTP/OAuth MCP in repository config. Follow its
current routes rather than broad search or candidate documents. Project Settings may point there
but is not parallel authority.

## Codex roles and shared engineering process

Codex has exactly two roles: **Coordinator** and **Worker**. Research, design, and implementation are bounded Worker functions. Independent review is a bounded function normally assigned to a Worker; an explicitly assigned Coordinator may perform it only when free of candidate authorship or material participation in designing, shaping, implementing, or dispositioning that candidate. Role name alone neither grants nor removes review eligibility.

- **Coordinator:** unbound orchestration across authorized work. It may fork/assign Workers, reconcile lanes and shared surfaces, acquire independent review or perform an exact review when explicitly assigned and free of candidate authorship or material participation in designing, shaping, implementing, or dispositioning that candidate, challenge disproportionate or unsupported worker/reviewer output, and carry authorized delivery through integration/landing. It does not gain product/effect authority merely from coordination or review.
- **Worker:** exactly one bounded task/work item at a time. A Worker performs the assigned research/design/implementation/review function inside that task's authority and current governing package; it does not self-expand scope or convert reviewer suggestions into requirements.

Shared process semantics apply across Codex and ChatGPT even when host mechanics differ:

### Human Input and Human Review

Before a consequential choice hardens, follow [Human Input](docs/human-input.md). Every material implementation still gets final live [Human Review](docs/human-review.md) before dispatch. Human Review approval covers only the exact reviewed package and does not authorize later effects; after approval stop and report exactly `Human Review approved. Dispatch ready: <WorkId>.`.
- **Review lifecycle:** the exact source work owner/requester owns the review outcome watch through terminal verdict unless ownership is explicitly transferred with attributable pickup. A mechanical router/Coordinator may own reviewer acquisition/routing, but SENT/registration/readback is not completion. While the runtime can read/wait/read, poll the exact review until terminal verdict, Marco stop/pause/reassignment, or a real access/execution blocker.
- **Findings are evidence:** a material finding must establish defect, evidence, consequence, affected claim/path, and minimum clearing condition. Reviewer remedies are advisory unless independently required by the governing contract. Challenge unsupported or disproportionate findings/remedies rather than adopting them mechanically.
- **Situation-review triggers (V1):** raw confidence alone never triggers a situation review. Exactly one independent bounded review is mandatory for an exact situation only after a mechanically established contradiction in required currentness or an applicable invariant, or evidence that a consequential effect is ambiguous. Other conditions are advisory warnings which require owner acceptance before review: (1) explicit low confidence about a named consequential decision must identify the decision, affected path, specific uncertainty, supporting evidence and next falsifier; (2) a missed expected checkpoint must identify the exact WorkId/run/review/operation, expected observable condition or next-check time, evidence of last material progress and current owner; (3) repeated failed attempts must identify the exact attempts, outcomes, shared objective and recurring blocker; and (4) evidence conflicting with active plan/status must identify the exact current claim and revision plus the contradictory evidence and its currentness. Missing evidence is `UNKNOWN`/`INSUFFICIENT_DATA`, never "stalled". Deduplicate by exact situation identity plus evidence set.
- **Situation-review result (V1):** the reviewer returns to the exact owner a verdict, current truth labeled `CURRENT`, `HISTORICAL` or `UNKNOWN`, a causal challenge, the smallest safe next action, the affected path, remaining unknowns and the exact Marco decision needed, if any. The reviewer has no effect authority and may not request or trigger another situation review. The owner takes an authorized action, records a material challenge with counterevidence, or pauses/escalates when consequential uncertainty remains; nonmaterial disagreement needs no rejection record. Pause only the affected consequential path; live mitigation and unrelated authorized work continue. A situation review never recursively triggers another situation review.
- **Durable continuity:** unresolved reviews/messages/watches and other material open obligations must remain represented in current durable work state with exact identity, owner/purpose, current state, next action, and terminal/unblock condition. Re-entry reacquires those exact obligations; do not reconstruct them from generic inbox/history scans.
- **Supervisory proportionality:** Coordinator challenge of scope growth, review overreach, test/qualification ratcheting, or unnecessary machinery is supervision, not a second substantive review and not new authority.

For explicit nontrivial ordinary-session implementation through landing, the Coordinator normally assigns implementation to a bounded Worker in an owned writer and independent review to a different eligible Worker for the immutable candidate. If the exact review is instead explicitly assigned to Coordinator and Coordinator is free of candidate authorship or material participation in designing, shaping, implementing, or dispositioning that candidate, Coordinator performs it directly; do not acquire a second reviewer merely because the reviewer is Coordinator. Findings return to the implementation Worker for disposition and to the same reviewer for focused rereview. The source work owner keeps the review outcome watch; reviewer acquisition/routing may be separately owned. Slice mutations by writable surface and semantic concern; keep one owner for each, one integration owner for shared surfaces, and every child terminal or explicitly stopped before completion. If lanes overlap or conflict, suspend the later mutation, reconcile any effects, then re-slice or serialize it. Once a candidate exists, drive authorized review, CI, correction, and landing steps to a terminal outcome without waiting for repeated prompts.

Target advancement alone does not require rebasing. Apply the candidate/current-target assessment in [code quality](docs/code-quality.md), preserving the immutable candidate and review evidence when exact composition can establish safety. Rebase only when needed or materially helpful; a rewritten head is a new exact candidate.

For substantive code/config/test work or Code Review, reopen `docs/code-quality.md` at the exact candidate/control SHA and perform only its four-question Refresh check. Implementers refresh on entry/re-entry/context replacement, before first material commitment, between distinct material slices, after failed hypotheses/material failures/findings/accepted corrections, before material shape changes, and before readiness/handoff/completion claims. Reviewers refresh on entry/re-entry/context replacement, before substantive review, after candidate/evidence/currentness or focused-correction changes, and before verdict/handoff. Use that document's finding, correction, qualification, decomposition, and landing/activation rules; do not improvise if it is unavailable.

Before material editing choose the smallest independently useful, valid, reviewable, and recoverable PR/layer shape. Stop and classify new material authority, persistence, concurrency, security/trust, recovery, external-interface/effect, or other hard-to-reverse choices. Before publication or landing claims, run affected tests and every claim-specific gate against the claimed subject. Keep inert landing separate from live activation/reliance.

At grounding, record whether the outcome is inert landing, activation/reliance, or both, and any HOLD/DENIED. Preserve pending activation with exact owner, target/revision, prerequisites, authority, proof, and return trigger. Keep Marco updated at meaningful semantic checkpoints, not on a timer. Lead with the work title; use opaque identifiers only when useful.

Activating any client-visible ChatGPT MCP schema or metadata change requires Marco to reinstall the ChatGPT app/connection, then start a fresh chat and verify the exact exposed schema and affected behavior. A server restart, authenticated `tools/list`, connection refresh, or fresh chat without reinstall is insufficient. Follow [ChatGPT MCP edge](docs/chatgpt-mcp-edge.md) for the canonical procedure.

When changing canary behavior/lifecycle, reconcile its durable procedure, current record, agent entry guidance, and propagation evidence; state alone is not installed instruction. When routed procedure requires Human Review for a protected effect, present the practical change, consequences/limits, and exact approval scope.

Detailed owners and procedures live in [architecture](docs/architecture.md), [development](docs/development.md), [code quality](docs/code-quality.md), [north star](docs/north-star.md), [the historical roadmap snapshot](docs/roadmap.md), [research sources](docs/research-sources.md) (where to research a task, and when to escalate), and current routed work. Live Switchstand/current work, not the roadmap snapshot, owns current status and priority. These references constrain execution; they never grant it.

Material work using the Implementation Specification process already carries its own documentation-impact requirement (the DOCS-CURRENTNESS DELTA under Asana 1218218678917862): this paragraph is the ordinary-work backstop, not a second rule for the same work. Implementation that changes something [architecture](docs/architecture.md) describes must update that doc in the same change; a material shift in near-term priority belongs in current routed work, not the historical [roadmap snapshot](docs/roadmap.md). [North star](docs/north-star.md) is a durable guiding reference: nothing routine touches it — it changes only via its own stated review process (independent critique + Human Review) described in the doc itself.
