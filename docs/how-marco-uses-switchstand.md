# How Marco uses Switchstand

Switchstand coordinates bounded engineering work across ordinary Codex, task-bound Codex workers, and ChatGPT. Marco is its sole human developer and operator; every other participant described here is an AI agent. He personally coordinates dozens of active or resumable agent threads. This page is the current usage model. It does not grant authority, replace a task's current governing package, or enumerate a volatile tool inventory.

## Start from current truth

Use the repository's current `main`, executable contracts, and canonical owner documents for implementation truth. For assigned work, use the exact current task, its current governing references, and the exact candidate/control revision. Superseded specs, old candidate branches, historical comments, and `marcogallotta/switchstandold` are evidence only unless a current route explicitly selects them.

Authority is separate from technical truth. It comes only from Marco's direct active assignment or an explicit CURRENT grant bound to the exact identity, surface, and effect. Code, documentation, project placement, role, and tool access cannot manufacture it.

If no exact task or WorkId is already assigned, resolve `START HERE` `1218327002478382` through
the authenticated provider-neutral `switchstand` HTTP/OAuth MCP for navigation only. It may
identify current routes or candidate WorkIds, but it cannot assign work, select focus, transfer
ownership, or grant an effect. Do not substitute broad provider search or a remembered/candidate
route, and do not start substantive work until Marco's direct assignment or an explicit current
grant names the exact WorkId. For an unbound Coordinator, navigation without that trusted binding
remains `COVERAGE_GAP / UNKNOWN` rather than recovered focus.

When that route or Marco requests meaningful root-cause analysis, use the canonical
[root-cause analysis procedure](root-cause-analysis.md). It keeps one sanitized parent WorkId,
routes corrections to their owning products, and shares process semantics across ordinary
ChatGPT and Codex without pretending their mechanics or inactive-session behavior are identical.

## Three working modes

### Ordinary Codex Coordinator

From the canonical repository, raw `codex` enters through a materialized host shim that delegates to `scripts/codex-dispatch`; outside the repository the same shim directly launches the real Codex binary without depending on checkout health. `scripts/install-codex-shim` installs or updates that host-owned file. The in-repository result is an ordinary, unbound Coordinator session with the isolated Coordinator environment and canonical Switchstand MCP. The default `PILOT` launch route registers that exact authenticated Codex thread (reusing an existing mailbox, including `/root`, without takeover) and supervises Wakeful only for the Codex process lifetime; Codex or supervisor exit stops intake while durable messages remain queued for the same mailbox. `SWITCHSTAND_CODEX_WAKEFUL=OFF` restores direct launch as the rollback. Directly operated Root and sub-Coordinators use native full local-user execution with `approval_policy=never`, including host user-session IPC and service-manager access. Repository policy forbids shared-primary mutation, and hooks block recognized Git and editing-tool mutation routes, but full local-user execution is not complete shell or operating-system isolation: arbitrary local-user shell code remains technically capable of writing the checkout. Implementation must stay in owned linked writers. Built-in Workers get repository-read-only shell access only through the generation-bound `codex-worker-inspect` command; arbitrary shell, file editing, and auto-memory writes remain denied because those Workers share Root's filesystem profile. New-generation Coordinators maintain `friction.md` through the validated symlink in their generation-owned writer, backed by the dedicated writable local-state directory. Malformed friction bookkeeping is quarantined and recreated visibly instead of blocking unrelated launch work. Pre-existing generations may retain the legacy primary-checkout binding during migration; that does not authorize new sessions to mutate the primary checkout. It has no launch-bound WorkId and gains no provider, deployment, activation, or other consequential-effect authority from its execution environment. Raw `claude` behaves the same way through `scripts/install-claude-shim` and `scripts/claude-dispatch`.

Switchstand-authored policy remains subordinate to Marco. A Coordinator executes ordinary
reversible direction, warns briefly and continues through bounded risk unless material ambiguity
requires confirmation, and challenges seriously destructive or irreversible action. Marco can end
that policy veto with an explicit override or equally clear waiver after the warning: record the
exact gate or risk waived, perform only the exact technically possible action, and read it back.
Anger, urgency, or emphasis alone is not override; platform prohibition, technical impossibility,
and external rejection remain real.

Mailbox registration or same-principal `/root` takeover restores only the durable messaging
address; it does not recover or grant a WorkId. An ordinary Coordinator grounds work by reading
the exact WorkId in Marco's direct assignment or in an acknowledged addressed handoff. It must
not call bare `work_get` to guess a current focus. If neither trusted source supplies an exact
WorkId, the truthful result is `COVERAGE_GAP / UNKNOWN`, not an inferred assignment. Bare
`work_get` remains the launch-bound managed-worker behavior described below.

Codex has two roles: Coordinator and Worker. The Coordinator can fork/assign Workers for bounded research, design, implementation, or independent review functions; those functions are not additional roles. It keeps disjoint lanes moving, challenges unsupported or disproportionate worker/reviewer output, reconciles qualification/current-target composition, and carries authorized work through integration/landing. Its orchestration model is intentionally different from ChatGPT and must not be copied there merely for parity.

#### Setup and workflow failure journal

Immediately record every newly observed setup or workflow failure through the repo-local,
Git-ignored friction register. `friction.md` is its short policy and category index; detailed entries
live in `friction/capability.md`, `friction/process.md`, or `friction/implementation.md`, selected by
the primary clearing action rather than symptoms or keywords. Capability covers access, tools,
runtimes, and environment; process covers routing, ownership, coordination, and procedure;
implementation covers repository code, configuration, and tests.

Use `scripts/friction-register has <stable-key>` to check the global exact-key registry without loading
the category corpora. Add an entry with `scripts/friction-register append <category> <stable-key>` and
the bounded entry on standard input. One entry is at most 800 Unicode characters and eight nonblank lines including its stable
key heading. It states only the attempted claim, observed result, state-change truth, and smallest
clearing action. A proven unfixed item that is not selected remains recorded with `Deferred
because` and `Evidence` lines. Exclude speculative backlog, transcripts, raw logs, portfolio
state, priorities, and secrets. Evidence detail belongs on the owning WorkId or in a durable
private artifact.

The writer serializes concurrent updates, checks exact headings without returning corpus prose, and
atomically replaces only the selected ledger. Existing unstructured index bytes are preserved;
initialization never silently rewrites them. An authorized activation runs `scripts/friction-register
migrate-index`, which preserves those bytes once as `legacy-index.md` before installing the short
index. Before handoff reconcile the register against current truth. If the path is not writable,
preserve the same facts in the authorized work result or handoff.
Missing feedback capability does not block assigned work.

#### Coordinator handoff

The launcher records the canonical repository commit and control manifest at Coordinator start and injects their exact paths into developer context. Reread them after compaction and before handoff. For ordinary in-session currentness, the Coordinator calls `coordinator_currentness_get`; when it returns `SYNC_REQUIRED`, the Coordinator passes that exact `target_sha` to `coordinator_main_sync`, inspects the full effect result, and reads currentness back. It never asks Marco to relay `git pull`. Dirty, divergent, moved-target, or ambiguous cases preserve their explicit failure or `UNKNOWN` state. The outgoing Coordinator first preserves its exact open obligations in private durable state, then runs `scripts/coordinator-handoff <that-exact-start-commit-path> <obligations-file>`. The command refuses dirty or divergent canonical `main`, fetches through the canonical remote identity, fast-forwards and reads back the exact revision, and registers a pending handoff. Plain raw `codex` launches remain independent and never claim it. Automatic transfer is disabled until a separate explicit addressed-claim mechanism binds the exact pending handoff to a launcher-proven generation; registration and a later plain launch are not pickup. Until attributable successor pickup and acknowledgement through that mechanism, obligations have not transferred and the outgoing Coordinator must not retire. Any future addressed-claim implementation must update `AGENTS.md`, `docs/architecture.md`, and this owner together. Failures retain private evidence under `~/.local/state/switchstand/codex/handoffs`.

Keep the handoff to four fields: **Git boundary** (starting record, final commit, and range), **trigger/change**, **outstanding work**, and **flag as broken** (the newly changed behavior whose failure the successor must report).

### Managed Codex worker

`scripts/switchstand --active <task> -- <assignment>` starts a one-task Worker. Trusted launch state injects the active WorkId, bounded read-only references, managed principal/currentness, and available tools. The Worker begins and re-enters with `work_get(api_version="1")` without inventing a WorkId. It may perform the bounded research/design/implementation/review function assigned to that work, and may write only the active work unless an explicit CURRENT grant says otherwise.

The managed surface is not general workspace discovery and is not interchangeable with the ordinary HTTP/OAuth surface. Exact-candidate isolated launch is a separate qualification route and does not itself activate or deploy anything.

### Ordinary ChatGPT

ChatGPT uses the authenticated repository-configured HTTP/OAuth MCP. It works with admitted provider-neutral WorkIds, durable messages, and protected operations. A chat without a repository uses `repository_bundle_get`, accepts only `current`, verifies the advertised SHA-256, and materializes the bundle as a normal repository. It retries `refresh_pending` and never substitutes stale cache. Use bundled current `main`, or prove and check out the exact requested SHA for review. A chat with a normal checkout uses Git normally.

When Marco must connect, reconnect, or reinstall Switchstand in ChatGPT, give him the exact MCP endpoint URL in that same instruction (currently `https://laptop.tail46f0b9.ts.net/switchstand/mcp`). Give the new chat exactly one trusted WorkId as its continuation anchor. Never use a Coordinator-local filesystem path as the ChatGPT-facing handoff mechanism: ordinary ChatGPT cannot read that path, and a list of WorkIds obscures which work actually grounds the session. The one WorkId must come from Marco's direct assignment or an explicit `CURRENT` grant; it neither expands authority nor replaces internal Coordinator handoff bookkeeping. Without one trusted WorkId, report `COVERAGE_GAP / UNKNOWN`.

The desired user model is one chat equals one stable named agent identity. The visible name, authenticated OAuth principal, hidden ChatGPT chat identity, MCP session generation, and any WorkId are distinct. The HTTP edge reads `openai/session` from MCP call metadata; the mailbox stores a hash of that value and binds one visible name to each principal-and-chat pair. Distinct chats under the same OAuth principal can register distinct names, while one chat cannot register multiple names. Repeating registration from an already-bound session returns its existing immutable name rather than creating another name or performing a takeover. The agent does not supply or see the hidden chat value. An unavailable chat identity yields a recovery result; explicit same-principal takeover of a name fences the old chat. When Marco declares an agent under another authenticated identity dead, the replacement session uses `agent_transfer_request` and reports its request ID. A host operator then runs `switchstand-agent-mailbox-transfer <request-id>` with the edge database environment. That command approves only the exact recorded mailbox preimage and destination session; it preserves the endpoint and pending deliveries, increments generation, and blocks the old owner. A stale request or a destination session already bound elsewhere is terminal and must not be retried under a new ID without rereading current state. This describes the code contract, not a claim that the live client journey has been accepted. Because `agent_transfer_request` changes the client-visible MCP schema, activation still requires app reinstall, a fresh chat, and exact schema/behavior verification.

## Current observed use and continuity limit (Marco, 2026-10-06)

This is Marco's report of how he currently uses the system, not a prescription for the
future or proof that the written process runs reliably:

- He launches a ChatGPT Coordinator for reviews. He uses the Codex launcher for
  implementation and local tooling fixes that arise during that work. He also uses
  separate research agents. These are current use cases; the three technical modes
  above describe available host/launch boundaries rather than this allocation of work.
- Most of the time Marco has 2-6 Codex heads (concurrent top-level Codex sessions),
  usually with native forked agents, plus 4-8 active ChatGPT tabs and several more
  closed chats whose obligations may later resume. The top-level head count is not
  the agent count: together these sessions and forks make dozens of active or
  resumable agent threads that Marco alone coordinates.
- Codex can fork bounded reviewers and keep working in a comparatively long-lived
  session. Ordinary ChatGPT chats have a shorter active life. Marco repeatedly has
  to tell stopped agents to **resume** and prompt agents to follow up on reviews they
  dispatched. That manual scheduling and chasing is a current burden, not a desired
  Coordinator responsibility.
- Marco reports a roughly 45-minute ChatGPT stall/expiry as a host constraint.
  Switchstand does not currently establish an external inactive ordinary-ChatGPT wake.
  Durable state can support safe re-entry when a session is active again; it does not
  by itself restart an inactive chat.

The desired direction is that an active agent continues authorized work and exact
nonterminal review/message/activation watches without repeated prompts, and returns
to Marco for a substantive decision, an actual blocker, or a host limit it cannot
cross. The written continuation rules in [AGENTS.md](../AGENTS.md) and current routed
owners state that obligation. Their presence is not evidence that ordinary ChatGPT
agents consistently follow it. Current UI installation, runtime adoption, and
cold-replacement success require separate readback or behavioral evidence.

### Human attention is a designed interface

Explicit priority is durable HUMAN state, not a synonym for attention. Ordinary authenticated
ChatGPT may SET or CLEAR HUMAN WORK/PROJECT priority only while executing Marco's explicit current
direction. The V1 provenance identifies the authenticated workspace principal and ordinary
ChatGPT path; it does not cryptographically prove the exact utterance. Managed workers remain
AGENT-recommendation-only. Project priority is context rather than inherited member priority, and
attention, readiness, labels, recency, or noise never authorize inferring HUMAN priority.

Marco's attention is necessary; it is not something agents should avoid using. Some authority,
judgment, review, approval, installation, and Project Settings actions genuinely require him.
Those requests are nevertheless consequential because one human serves the whole portfolio. A
minute spent servicing one agent is unavailable to the other dozens; viewed per agent, the
available share of Marco's attention may be roughly one-thirtieth of what a one-to-one
collaboration would imply.

Design the system and every agent interaction around both truths. Ask promptly when Marco is
actually needed, and make the request exceptionally clear and immediately actionable. State the
exact action or decision first; provide the direct target, link, text, or command; recommend an
outcome where appropriate; explain why it is needed now and the consequence of delay; distinguish
what is blocked from what continues; and say how success will be confirmed. Complete all safe
preparation before asking and combine compatible requests when that makes the interaction easier,
but never hide, suppress, or defer an essential ask merely to reduce interruptions.

**Interactive polling.** When Marco says “poll” or “keep polling” for a current inbox or exact review, the agent reads that exact surface, uses a supported bounded wait, and reads again while the current session can run. An empty read or an arbitrary count of checks is not a terminal result. A ChatGPT Scheduled task, including an hourly condition watch, is a different future-running product and must not replace this live obligation unless Marco explicitly requests scheduled future runs. At a real host limit, preserve the exact watch and state that active polling stopped; the inactive chat cannot promise further checks, and the obligation resumes on active re-entry.

### Example: one review obligation

This illustrates the intended handoff of responsibility, not a claim that the
complete journey has passed live acceptance.

1. An owner requests one exact bounded review and records its identity, purpose,
   owner, state, next check, and terminal condition on the current durable work item.
   A successful send establishes **SENT**, not reviewer pickup or a verdict.
2. While its session can run, the owner checks that exact review after bounded work
   or a supported wait. It does not ask Marco to poll or search a generic inbox.
3. Before a chat stops, the process requires the outstanding watch on the durable
   item. On later active re-entry, a replacement reads that exact item and resumes
   the watch. If it was never saved, recovery is UNKNOWN. Routine ChatGPT recovery
   has not been proved here.
4. On a verdict, the owner checks the finding and evidence, sends any bounded
   correction for focused rereview, and continues authorized delivery until the
   review and dependent work reach a truthful terminal state. A review PASS supplies
   evidence, not new scope or effect authority.
5. Marco is asked when a material choice or approval genuinely belongs to him.
   An inactive host cannot be polled or awakened by these instructions.

## Identities and routing

- A **WorkId** is Switchstand's stable application identity for work. A provider task GID is an adapter identity and never substitutes for a WorkId or grant.
- An **agent name** is the visible durable messaging address. Current mailboxes bind the name to a principal and hidden chat identity; mailbox generation fences explicit takeover. A name or chat identifier does not grant work authority.
- An **OAuth principal** authenticates the ordinary caller. Authentication establishes who is calling, not which work or effect is authorized.
- A **runtime session/run** supplies currentness fencing. A replacement session must recover explicitly rather than silently continuing received work.
- **Project membership and fields** route discovery. They do not change task/WorkId identity, reassign ownership, or grant an effect.

Keep one semantic owner for each real concern. Preserve the same task GID and WorkId across area migration, and use the current area registry only to find work whose exact identity is not already known.

## Work, messages, and feedback

Durable message tools and state are the current agent-to-agent surface. Ordinary interactive agents address registered names through `agent_message_*`; managed task-bound runtimes retain WorkId-addressed `message_*`. Sending, availability, receipt, recovery, result, disposition, provider effect, and completion are distinct. A sender's success is not recipient pickup. Active agents check their exact pending/review surfaces during bounded work and on re-entry; Switchstand does not promise an inactive wake, generic inbox scan, or background daemon.

Ordinary current intent, progress, findings, verdicts, and results belong in
notes through `work_update`. `work_append` is only for explicitly purposed
provenance, investigation, or legacy reconciliation on writable admitted work;
it is not current state or agent messaging, and its ordinary and managed
contracts differ. An ambiguous possible effect remains UNKNOWN and must be
reconciled rather than resent under a new identity.

Provider source and attachment tools are retired. Use WorkId-based reads and local legacy-reference resolution.

Messages and readable records are evidence and requests. They cannot grant permission, reassign an agent, prove recipient pickup, or establish that an external effect happened.

## Shared process semantics across hosts

ChatGPT and Codex deliberately use different coordinator, delegation, task-binding, and repository mechanics. Align only the underlying process contract:

- surface Human Input before consequential expansion/hardening while Marco can still cheaply change direction;
- keep independent review exact and challenge findings/remedies rather than treating reviewer suggestions as requirements;
- the source work owner/requester keeps the exact review outcome watch through terminal verdict, while a router/Coordinator may separately own reviewer acquisition;
- preserve unresolved material obligations and exact nonterminal reviews/messages/watches in durable current state so replacement does not depend on chat memory or comment archaeology;
- use exactly one independent bounded situation review for an exact situation only when a mechanically established required-currentness/applicable-invariant contradiction or evidence of an ambiguous consequential effect mandates it, or when the owner accepts an advisory warning. Raw confidence alone never triggers it. Advisory warnings require: a named consequential decision, affected path, uncertainty, evidence and next falsifier for explicit low confidence; exact WorkId/run/review/operation, expected observable or next-check, last-progress evidence and owner for a missed durable checkpoint; exact attempts, outcomes, shared objective and recurring blocker for repeated failure; or an exact current plan/status claim and revision plus contradictory evidence and currentness for an evidence conflict. Missing evidence is `UNKNOWN`/`INSUFFICIENT_DATA`, not "stalled"; deduplicate by exact situation identity plus evidence set;
- require the situation reviewer to return to the exact owner a verdict, `CURRENT`/`HISTORICAL`/`UNKNOWN` truth labels, causal challenge, smallest safe next action, affected path, remaining unknowns and exact Marco decision if any. The reviewer has no effect authority and cannot request another situation review; the owner acts, materially challenges with counterevidence, or pauses/escalates consequential uncertainty. Pause only the affected path while live mitigation and unrelated work continue, and never trigger reviews recursively;
- challenge disproportionate scope, machinery, review, and qualification growth without turning supervision into a second substantive review;
- preserve explicit authority/currentness, proportional evidence, final Human Review where required, and protected-effect boundaries.

Shared semantics do not imply identical host storage, tools, roles, or orchestration.
The [root-cause analysis procedure](root-cause-analysis.md) applies this same boundary to
evidence lanes, checkpoints, correction watches, and matching-event validation.

## Repository delivery

Ordinary source work happens in an owned linked writer based on an accepted exact green SHA; the shared primary checkout stays read-only apart from fetching and writer creation. Managed workers use the trusted task-private writer supplied by launch.

Review and tests name their exact subject. Candidate review, exact-head checks, current-target composition, landing, deployment, and live reliance are different claims. Target advancement alone does not require a rebase: preserve the immutable reviewed candidate when composition can establish safety, and rebase only when needed or materially helpful. A rewritten head is a new exact candidate.

Use [development](development.md) for commands and evidence subjects, [code quality](code-quality.md) for implementation/review rules, and [architecture](architecture.md) for current owners. `AGENTS.md` remains the load-bearing bootstrap and router.
