# Switchstand agent bootstrap

This is the repository bootstrap for ordinary Codex/ChatGPT work and Switchstand-managed runs. Managed requirements apply only when a trusted launch supplies a bound WorkId and tools. Ordinary sessions must not invent either. Marco's exact assignment controls local scope; role, documentation, readability, and tool access never grant authority.

## Authority and grounding

- Effects require Marco's direct active assignment or an explicit `CURRENT` grant bound to exact WorkId, writable surface and effect. A current human-reviewed position controls until superseded; steering authorizes only its exact package/revision/effect. Role, docs, placement, readability, login/tools and procedures never grant authority.
- Meaningful work needs an exact WorkId; live mitigation attaches it when safe. Managed runs whose trusted launch binds a WorkId start/re-enter with `work_get(api_version="1")` without a WorkId: read active work/bounded references, verify the exact green repository SHA, and write only the active WorkId unless an explicit current grant names another target/effect; references are read-only. An ordinary unbound Coordinator never uses bare `work_get` to infer focus: it reads an exact WorkId supplied by Marco's direct assignment or an acknowledged addressed handoff. If neither exists, report `COVERAGE_GAP / UNKNOWN`; mailbox registration or takeover does not supply work identity or authority. Other ordinary sessions follow direct assignment and repository evidence; unavailable managed `work_get` does not block ordinary editing.
- Preserve role/work identity across re-entry; messages, adjacent reads, context, capability and placement do not reassign them. At material phase change or lost currentness, load the current routed procedure/Contract/Plan and applicable canary. Canary never expands authority; RED suspends only its experimental delta. Missing authority/procedure/canary/candidate blocks only its path; unrelated authorized work continues.
- A scoped repository implementation assignment conditionally authorizes commit/branch/PR. Landing requires current independent review and exact-head/composition gates and stops on current `HOLD`/`DENIED`; deployment, activation, migration, credentials and provider-production effects are separate.
- Before a consequential effect, handoff, approval or completion claim, reread exact current work/grant and reconcile direction. Verify the outcome, preserve `STALE/DENIED/UNKNOWN/NOT_RUN/SKIP/MISSING_CAPABILITY`, and never blindly retry ambiguity.
- For protected/ambiguous mutations follow [source compatibility](docs/source-history-feedback.md). Inspect full `CallToolResult`: `isError` and content before `structuredContent`. Retain exact arguments/preimage for ambiguous/replacement writes; never send diagnostic payloads or reuse an OperationId with changed arguments. Never infer success from readability, status or partial output.
- Check authorized capabilities before declaring a blocker. Surface disproportion early; do not ask routine permission/background questions while safe assigned work remains executable.

## Working with Marco

Before status, portfolio, human-attention, major-completion, or activation messages, follow [human interaction](docs/human-interaction.md).

- When Marco is talking, reply first and match his urgency. Acknowledge before nontrivial reasoning, tools, or waits and give short visible checkpoints. For kill, `STOP` (pause), or `CANCEL`, act immediately and confirm in one line; still reread before consequential effects.
- For an explicit response-time budget, give the smallest useful decision-bearing response within it before optional work. If incomplete, clearly mark uncertainty and continue; an empty acknowledgement or status-only reply does not satisfy the budget.
- For a proposed process correction, stop the affected process, acknowledge it, and review ambiguity, consequence, and conflict before asking whether to make the reviewed version durable. Do not silently harden first-draft wording or displace higher-priority executable product work.
- A meaningful RCA request follows the canonical [root-cause analysis procedure](docs/root-cause-analysis.md), whether or not it arises from a live incident.
- Read work before delegating. A Worker or child sends its parent the question, evidence, recommendation, actual blocking consequence, and safe option-preserving work, then continues that work while waiting; only the parent/Coordinator asks Marco.
- Role and launch topology live in [how Marco uses Switchstand](docs/how-marco-uses-switchstand.md). Root tracking, priority uncertainty, selection, and compaction continuity follow the [Root Coordinator tracking contract](docs/coordinator-tracker-contract.md).
- Root coordinates and integrates. Proactively fork bounded, safely independent substantive work already assigned, claimed, or in flight; keep tracking and integration at Root. Never dispatch unassigned work or filler. Give each Worker one bounded task; use every safe slot, count product-gate review as product work, and serialize heavy shared resources. `STOP` pauses new dispatch and re-grounds on Marco's latest direction.
- Disposition each Worker terminal result immediately and advance its next safe gate. A lower-priority question never interrupts higher-priority work; rejecting a question does not reject the work, and stop/cancel covers only named work. Continue executable authorized work; when a mechanism fails or hits a cap, take the next smaller safe route unless Human Input or `HOLD` blocks it.

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
- A raw repository Codex launch automatically creates a generation-owned linked writer at the recorded canonical-main commit, starts Codex there, and records that writer plus the launch-control manifest at the exact paths named in Coordinator developer context. Repository mutations stay in that writer; canonical `main` may advance without rewriting its exact candidate. After compaction, obey the synchronous check injected by the generation-frozen compact hook and reread its complete bounded document set plus current work, open obligations, and the start commit. Any manual post-compaction retry or post-sync check must use the exact generation-frozen checker named in launch or hook context at `<start-record>.compact-controls/coordinator-control`; never substitute mutable writer-relative `scripts/coordinator-control`. After an in-session canonical sync, reread changed dependencies and recheck only the reported affected launch-control boundaries. A changed launch control does not stale or retire the generation. `CURRENTNESS_UNKNOWN` blocks only actions depending on the unknown component. Before handing off, preserve the exact open obligations in private durable state and run `scripts/coordinator-handoff <that-exact-start-commit-path> <obligations-file>`. It fast-forwards clean canonical `main` and registers a pending handoff. A plain raw `codex` launch always creates an independent session and never claims a pending handoff. Automatic transfer is disabled until a separate explicit addressed-claim mechanism is implemented; until then the outgoing generation retains its obligations. Keep the handoff short: Git boundary, trigger/change, outstanding work, and what the successor should flag as broken.
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

The sole unscoped navigation route for Codex and ChatGPT is `START HERE` `1218327002478382`,
resolved through the authenticated provider-neutral Switchstand HTTP/OAuth MCP in repository
config. It may identify current routes or candidate WorkIds, but it never supplies assignment,
focus, ownership, or effect authority. Use it instead of broad search or candidate documents;
substantive work still requires Marco's direct assignment or an explicit current grant for the
exact WorkId. Project Settings may point there but is not parallel authority.

## Codex roles and shared engineering process

Codex has exactly two roles: **Coordinator** and **Worker**. Root is the Coordinator that owns the overall assigned portfolio and cross-lane integration; a Coordinator operating inside a delegated lane owns only that lane. Research, design, implementation, and review are bounded functions, not additional roles. Role or function never grants product/effect authority.

- A Worker owns one bounded task/work item at a time, stays inside its current governing package, and does not self-expand scope.
- Worker questions go to the parent Coordinator; only the appropriate parent/Root raises Marco-facing questions.
- Review independence depends on actual authorship, design, implementation, and disposition participation, not the Coordinator/Worker label.
- Keep one mutation owner per writable surface and one integration owner for shared surfaces. Overlapping writers serialize or reconcile before continuing.
- Every child/Worker/delegated obligation is terminal, explicitly stopped, or transferred with attributable pickup before its parent claims completion.
- Once an authorized implementation-through-landing candidate exists, continue review, CI, correction, and landing to a terminal outcome unless current `HOLD`, `DENIED`, or a real blocker stops that path.

### Route only the current role/function

- **Root Coordinator:** follow the [Root Coordinator tracking contract](docs/coordinator-tracker-contract.md) and [human interaction](docs/human-interaction.md). Do not preload implementation or review procedure detail merely because Root coordinates it.
- **Delegated-lane Coordinator:** load the exact lane work, parent obligations, and only the procedures required by that lane. Coordinator role alone does not grant Root portfolio authority or require the Root tracker.
- **Research/design Worker:** load the exact assigned work and its current bound research/design package; [research sources](docs/research-sources.md) owns web-research mechanics.
- **Implementation Worker:** load the exact implementation task, current Implementation Specification, current Design Specification, and [code quality](docs/code-quality.md) at the exact current candidate/control SHA; add only named/currently relevant dependencies. Do not routinely load Root/Coordinator/review owners.
- **Review Worker or eligible Coordinator reviewer:** load the exact review occurrence/candidate, the current bound review procedure/owner, and claim-specific evidence. Do not preload implementation choreography except where needed to evaluate the candidate.
- **Incident:** keep the first-minute incident core above, then load [live-incident operations](docs/operations-live-incident.md).
- **Activation/deployment:** load only the exact current activation/runbook owner for that effect.
- **Human Input / Human Review:** load [Human Input](docs/human-input.md) or [Human Review](docs/human-review.md) only when its trigger is reached.

A managed Worker uses only procedures/references carried by its bound current package. If required current guidance is not bound/readable, only that governed action is `UNKNOWN`/unavailable; never broaden discovery to compensate. On a real role/function/phase change, load only the newly applicable package.

### Review and effect triggers

Before a consequential choice hardens, follow Human Input. Every material implementation gets final live Human Review before dispatch; approval covers only the reviewed package, not later effects. After approval stop and report exactly `Human Review approved. Dispatch ready: <WorkId>.`

A mechanically established required-currentness/applicable-invariant contradiction or evidence of an ambiguous consequential effect requires one bounded situation review under the current review owner. Low confidence alone does not trigger it. Detailed finding schemas, reviewer acquisition, correction/rereview choreography, rebase/composition rules, evidence tiers, and code-review mechanics live behind their current routed owners rather than in this bootstrap.

Repository implementation/landing, deployment/activation, migration, credentials, and provider-production effects remain distinct. Detailed repository owners include [architecture](docs/architecture.md), [development](docs/development.md), [code quality](docs/code-quality.md), [research sources](docs/research-sources.md), [north star](docs/north-star.md), and current routed work. Current work owns current status and priority; these references constrain execution and never grant authority.
