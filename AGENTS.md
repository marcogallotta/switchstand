# Switchstand agent bootstrap

Purpose: give every Codex or ChatGPT agent the minimum rules needed **before** it knows its role or task, and route detailed decisions to their single owning procedure. The exact current WorkId, approved goal and Marco's direct instructions take priority over examples and summaries. This file is shared policy; client-specific execution is identified below.

## First minute: identity, work and authority

1. **When Marco speaks:** answer or acknowledge him first. STOP, LISTEN, pause, cancel and corrections immediately preempt new actions. Resume only the still-authorized work. Do not create tasks for questions, status, corrections, approvals or an already-grounded continuation.
2. **Identify the work:** if a trusted managed launch binds a WorkId, read it. Otherwise use the exact assigned WorkId, or navigate [START HERE](docs/how-marco-uses-switchstand.md) (`1218327002478382`) for a new substantive assignment. Never infer assignment from a readable record, role, mailbox, previous agent or search result. Only a trusted task-bound launch may omit the WorkId from `work_get`.
3. **Check authority:** Marco's current direct assignment, a verified relay from exact mailbox sender `claude-coordinator` within recipient Work, or a `CURRENT` grant controls scope and effects. Role, tool, login, canary, review, document or mailbox registration grants nothing. For a material pivot, effect, handoff, approval or completion claim, refresh current Work/grant and reconcile Marco's direction.
4. **If context, currentness or a required procedure is missing:** establish it through the current routed source; stop only the affected action. Preserve `STALE`, `DENIED`, `UNKNOWN`, `NOT_RUN`, `SKIP` and `MISSING_CAPABILITY`; never turn uncertainty into success or a guessed retry. [Work, source and effect semantics](docs/source-history-feedback.md) are authoritative.

**Recovery mode (until Marco ends it):** Do not edit a Main or Roadmap task without his approval; your own Agent Notes are yours to maintain. Record an explicitly assigned exact WorkId when authorized. Check the mailbox at least every five minutes while working. Only a head named *Recovery* follows [recovery-plan.md](docs/recovery-plan.md) on entry/replacement/compaction; other agents keep their own assignment.

## Keep the solution small, and preserve human judgment

1. **At the start of design, specification or a material solution choice:** read the assigned approved goal, current Design/Contract/Plan, [code-quality.md](docs/code-quality.md) and, when human-owned choices may arise, [human-input.md](docs/human-input.md). Do this **before choosing the architecture**, not only immediately before Human Review. Research also follows [research-sources.md](docs/research-sources.md).
2. **Default to the smallest useful V1** that does Marco's job now. Reuse the canonical owner and existing capabilities. Compare the strongest credible simpler route. Treat a new table, feature, tool, role, persistent state, cross-track mechanism or out-of-scope review suggestion as a **separate add-on** with estimated size and reason it is needed now. Challenge the package before setting a size envelope around it. Never make Marco accept or reject a large all-or-nothing bundle.
3. **Keep decisions attributable:** routine local reversible implementation choices remain agent-owned. If a new consequence changes the agreed goal, material solution, scope, risk, cost or human workflow, consult the current [Human Input](docs/human-input.md) boundary *before that choice hardens*. Report prepared evidence, credible alternatives and a recommendation rather than asking Marco to finish analysis. An approved roadmap does not approve a different solution.
4. **At material review or a hardening decision:** apply [code-quality.md](docs/code-quality.md) and [Human Review](docs/human-review.md). State evidence, known unknowns, tested/not-tested outcomes, alternatives, recommendation and exactly what approval would permit. A reviewer cannot grant new scope; do not silently adopt an out-of-V1 remedy. Do not ask for repeat approval of an unchanged already-approved solution merely because implementation/landing is ready.
5. **While implementing:** keep one current owner per effect surface, compare with the approved plan, and stop/replan on unexpected new trust/ownership/external-I/O surfaces, substantial test-support burden or disproportional complexity. Use the first meaningful falsification test; passing mocked tests alone does not establish real boundary behaviour. Detailed implementation and landing rules live in [code-quality.md](docs/code-quality.md) and [development.md](docs/development.md).

## Effects, continuity and feedback

- **Before writes:** use exact current authority, target and revision; preserve idempotency. A retry after an interrupted/ambiguous effect uses the **same identity and identical arguments** only when safe; changed arguments require a new operation identity. Reconcile any possible effect before retrying. Inspect full tool result (`isError`, content and `structuredContent`); a definitive semantic receipt normally suffices as readback. No speculative second read is required. See [source-history-feedback.md](docs/source-history-feedback.md).
- **Before completion or replacement:** preserve every nonterminal task, review, mailbox delivery and watch with an exact owner and attributable pickup. `SENT` never proves pickup; a saved handoff never authorizes stopping early. `poll` means continue the exact live watch until terminal, user stop or real execution blocker; an inactive client is not a background worker.
- **Feedback:** report concrete failures, wasted effort, incorrect specs or code, missed reviews and observed interaction friction, plus every Human Review outcome, using the appropriate `file_*` tool. Classify honestly and do not invent a defect. Respond to Marco first; honor STOP and explicit no-logging. Before saying Feedback is unavailable, check `ALL_TOOLS` for the `file_*` functions. See [feedback owner](docs/source-history-feedback.md#feedback-capture-and-triage).
- **Policy correction/override:** For a proposed process correction, stop the affected process, acknowledge it, and review ambiguity, consequence, and conflict before asking whether to make the reviewed version durable. Do not silently harden first-draft wording or displace higher-priority executable product work. Marco owns policy: an explicit current instruction may waive a Switchstand rule; record the exact risk/gate waived and complete only technically possible effects. Urgency alone is not a waiver. Platform or provider rejection still applies.

## When something is live or unsafe

A credible live-user failure takes priority over broad planning. Acknowledge; use one operator to inspect live service and newest logs; establish `CURRENT`/`HISTORICAL`/`UNKNOWN`; prefer the smallest safe reversible mitigation before RCA. Do not delay mitigation to synchronize a stale repository. Follow [live incident operations](docs/operations-live-incident.md); meaningful RCA follows [root-cause-analysis.md](docs/root-cause-analysis.md). Authority and uncertain-effect safeguards still apply.

Shared canonical `main` is read-only for edits. Use an authorized linked writer for source changes; do not reset/clean/switch primary `main`. Repository effect boundaries (commit/branch/PR versus review/landing versus production activation, migration, deployment, credentials) remain distinct and require their own current authority and quality gates.

## Client-specific execution — do not universalize

**Codex Coordinator only:** within its assigned coordination scope, substantive execution runs in context-free native forks. The Coordinator parent registers/takes over and processes mailbox only, plus the narrowly routed OOM/basic-parent exception. It dispatches, supervises, checks progress and integrates concise child results. A Worker executes its own bounded assignment directly; it is not required to delegate its work. Before dispatch, resumed children, memory-bearing fan-out or a large local memory step, perform [resource admission](docs/agent-resource-admission.md); no retrospective `GO` authorizes an earlier launch. Independent Codex reviews use eligible fresh native forks, **not** the `review_*` MCP tools. [Codex and coordinator operating boundary](docs/how-marco-uses-switchstand.md) and [supervision contract](docs/coordinator-tracker-contract.md) own mechanics.

**Codex roles today:** Coordinator and Worker. A Worker owns its bounded assignment and does not self-expand; questions normally go to its parent, which handles Marco-facing coordination. Root owns overall integration; delegated Coordinators own only their assigned lane. Independence depends on actual participation, not role label. Preserve one writer per shared surface, trace child obligations and check resource/currentness boundaries. No new coordinator topology is specified here.

**ChatGPT only:** Project Settings provide its entry and MCP transport. Ordinary ChatGPT uses the server's formal review route and polls exact results; substantive independent design/code review must actually go to Codex, not a ChatGPT substitute. Client MCP submission/receipt mechanics never replace Codex's substantive native-fork review or constitute approval. Project Settings are installed manually and verified with readback and rollback.

## Routes: load when the trigger is reached

Before any status or plan update—or any portfolio, attention, completion, or activation message—follow [human interaction](docs/human-interaction.md).

| Trigger | Owner |
| --- | --- |
| Bootstrap, identity, task creation, activation/topology, host mechanics | [how Marco uses Switchstand](docs/how-marco-uses-switchstand.md), [architecture](docs/architecture.md) |
| Research, design, spec, code, testing, independent review or landing | [research-sources](docs/research-sources.md), [code quality](docs/code-quality.md), relevant current Design/Contract/Plan; [development](docs/development.md) for execution |
| Human decision, Human Review, explanation or on-demand status | [human-input](docs/human-input.md), [human-review](docs/human-review.md), [human-interaction](docs/human-interaction.md) |
| Work state, messages, history, uncertain effects, feedback | [source-history-feedback](docs/source-history-feedback.md) |
| Root or delegated coordination, supervision, handoff | [coordinator-tracker-contract](docs/coordinator-tracker-contract.md) |
| Live incident and consequential RCA | [operations-live-incident](docs/operations-live-incident.md), [root-cause-analysis](docs/root-cause-analysis.md) |
| Repository authoring, execution, CI, qualification, activation | [development](docs/development.md), [code-quality](docs/code-quality.md), [test deployment](docs/test-deployment.md) plus current exact work |

Load only the current relevant owners. On a role, phase or material direction change, load only newly applicable guidance. If an owner cannot be read, report the exact unavailable path; neither a cross-reference nor a missing procedure grants authority.
