# How Marco uses Switchstand

Switchstand coordinates bounded engineering work across ordinary Codex, task-bound Codex workers, and ChatGPT. Marco is its sole human developer and operator; every other participant described here is an AI agent. He personally coordinates dozens of active or resumable agent threads. This page is the current usage model. It does not grant authority, replace a task's current governing package, or enumerate a volatile tool inventory.

## Start from current truth

When agent reporting is deployed, use `agent_status_get` and page its exact-owner `work_search`
instruction across projects, reconciling actual assignments and known obligations. Report via
`agent_status_update`: finished with evidence, current, waiting-and-why/unblock, next authorized
action, priority/scope concerns and forgotten requests (capture grants no execution authority).
Use PARTIAL/UNKNOWN honestly; exhausted owner search is not complete remit. Named workspace
agents can read reports; only the current bound agent writes its own. No sensitive logs/secrets.
Check on entry/re-entry, material changes and before handoff/no-runnable claims. These are agent
responsibilities: server cues occur only on work_get, review_request/get and fresh mailbox polls,
when missing/old-binding or15 wall-clock minutes old. No offline wake or active-time monitor.
DEFER requires a concrete competing duty/recovery checkpoint and at most900 seconds; it preserves
report age. Fresh unchanged review polls need no repeated report. Continue authorized runnable
work while retaining the exact review watch; otherwise bounded same-review polling continues.
Replacement reads the preserved report AND current work. Status freshness never proves completion.

Use the repository's current `main`, executable contracts, and canonical owner documents for implementation truth. For assigned work, use the exact current task, its current governing references, and the exact candidate/control revision. Superseded specs, old candidate branches, historical comments, and `marcogallotta/switchstandold` are evidence only unless a current route explicitly selects them.

Authority is separate from technical truth. It comes only from Marco's direct active assignment or an explicit CURRENT grant bound to the exact identity, surface, and effect. Code, documentation, project placement, role, and tool access cannot manufacture it.

If no exact task or WorkId is already assigned, resolve `START HERE` `1218327002478382` through
the authenticated provider-neutral `switchstand` HTTP/OAuth MCP for navigation only. It may
identify current routes or candidate WorkIds, but it cannot assign work, select focus, transfer
ownership, or grant an effect. When Marco directly assigns new substantive work but supplies no
WorkId, resolve and read `START HERE` as its provider-neutral parent. If the parent exposes an exact
`canonical_root`, the child inherits it; because `START HERE` is an imported navigation-only
`Context` without one, each child starts a new exact canonical root while retaining `START HERE`
only as its placement parent. In either case create a minimally titled child with `work_type="Task"`,
`lifecycle_state="CURRENT"`, `owner_key="SELF"`, `wait_kind=unblock_condition=next_due="NONE"`,
`next_action_class="OWNER_CAN_DO"`, and an assignment-derived exact `next_action_ref`; omit
`canonical_root` and let the server resolve it from that exact parent. Do not use the legacy
minimal create form. Use one stable OperationId. Inspect the complete
create outcome and require exact created-ID readback. For a
possible-send ambiguity, replay only the identical OperationId and arguments as directed; never
mint a second create intent. This grounding step derives authority only from Marco's assignment; the route supplies
placement. Do not create a record for a simple question, status request, correction, approval,
already-grounded continuation, or inferred/contextual work. A denied, unavailable, or ambiguous
create after reconciliation remains `COVERAGE_GAP / UNKNOWN`. Do not substitute broad provider search or a
remembered/candidate route.

When that route or Marco requests meaningful root-cause analysis, use the canonical
[root-cause analysis procedure](root-cause-analysis.md). It keeps one sanitized parent WorkId,
routes corrections to their owning products, and shares process semantics across ordinary
ChatGPT and Codex without pretending their mechanics or inactive-session behavior are identical.

## Three working modes

### Ordinary Codex Coordinator

Raw `codex` is the official npm-installed CLI. In the repository it loads the repository instructions and authenticated Switchstand integration without a launcher-created writer, manifest, Wakeful supervisor, or launch-bound WorkId. A repository hook re-injects the existing Feedback V1 obligation for each user prompt and after compaction; it does not file feedback or create another reporting path. A session needing messaging registers one unique durable name; resume reuses the same binding. Only a stock session Marco directly addresses as `Codex Coordinator` calls `agent_takeover("Codex Coordinator", message=<Marco's full current message>)`. A first cross-principal attempt without that confirmation stops with `takeover_confirmation_required` and tells the agent to ask Marco; the retry passes Marco's full confirmation message. `/root` remains a temporary inbound alias for old senders. The Coordinator checks pending mail at the start and end of each Marco turn and whenever Marco says `check mail`, and never retakes the name after losing it without a new direct instruction. Repository policy still requires source changes in an owned linked writer. Raw `claude` retains its separate `scripts/install-claude-shim` and `scripts/claude-dispatch` path.

After compaction, the same hook first directs recovery of the ORIGINAL APPROVED objective,
current constraints/STOP and bounded agent notes from the exact assigned task; agent summaries
are not approval. Corrections must be recorded and approved on that task. Preserve role/WorkId,
read current role guidance (Root: the Coordinator tracking contract; others: their AGENTS route),
and load other docs only for the current function/phase/trigger; AGENTS is already supplied.

Switchstand-authored policy remains subordinate to Marco. A Coordinator executes ordinary
reversible direction, warns briefly and continues through bounded risk unless material ambiguity
requires confirmation, and challenges seriously destructive or irreversible action. Marco can end
that policy veto with an explicit override or equally clear waiver after the warning: record the
exact gate or risk waived, perform only the exact technically possible action, and read it back.
Anger, urgency, or emphasis alone is not override; platform prohibition, technical impossibility,
and external rejection remain real.

Mailbox registration or Marco-confirmed `Codex Coordinator` takeover restores only the durable messaging
address; it does not recover or grant a WorkId. An ordinary Coordinator grounds work by reading
the exact WorkId in Marco's direct assignment or in an acknowledged addressed handoff. It must
not call bare `work_get` to guess a current focus. For a new direct substantive assignment without
an ID, it follows the single-create-and-readback route above. If that route cannot establish an
exact WorkId, the truthful result is `COVERAGE_GAP / UNKNOWN`, not an inferred assignment. Bare
`work_get` remains the launch-bound managed-worker behavior described below.

Codex has two roles: Coordinator and Worker. The Coordinator can fork/assign Workers for bounded research, design, implementation, or independent review functions; those functions are not additional roles. It keeps disjoint lanes moving, challenges unsupported or disproportionate worker/reviewer output, reconciles qualification/current-target composition, and carries authorized work through integration/landing. Its orchestration model is intentionally different from ChatGPT and must not be copied there merely for parity.

#### Native fork operating boundary

ALL execution belongs in context-free native forks, including record/document reads and
Tracker, mailbox and feedback operations. The parent talks with Marco and actively supervises
concise checkpoints/results under the [Root tracking contract](coordinator-tracker-contract.md#supervise-live-children-from-attributable-evidence).
Workers execute their bounded assignments directly. Required resource admission belongs to
dispatch mechanics (only GO/WAIT reaches the parent); it must precede the exact action and
does not require launching another fork to admit its own launch. Missing evidence means WAIT.
Do not claim a repository instruction overrides a higher-priority mandatory main-agent
operation, disables host compaction, or supplies an unavailable native capability.

If an OOM or measured memory admission prevents a fork, the parent may perform basic bounded
checks directly: resource/admission checks, exact work or mailbox reads, and status inspection.
State the memory condition and keep these checks small. This exception permits neither
implementation, substantive investigation/testing nor independent review in the parent; it does
not authorize memory-heavy work, a host/cgroup change or bypassing admission for another fork.
Resume the normal fork route when memory allows it.

Fork Management records one concise checkpoint per bounded assignment plus material exceptions
and outcomes through the existing fixed `file_fork_management` feedback adapter; routine checks
do not each create a report. A logging receipt does not prove actual supervision. Its category
is `FORK_MANAGEMENT`; ordinary edge and managed profiles read
`SWITCHSTAND_FORK_MANAGEMENT_PROJECT_ID` for the existing
canonical project destination. Provisioning/configuring that destination and activating a
runtime are separate effects. Missing configuration truthfully denies reporting; source
landing does not establish collection. The later ticker is not part of this implementation.

The live native mechanics are bounded forks and messages while the parent Codex process is active,
plus the V1 writer binding described above: before implementation, Root binds the exact Worker,
WorkId, base, and non-overlapping repository-relative surface to a dedicated linked writer; the
Worker then uses only the fixed writer-bound inspect, test, and commit entry points. Keep one
mutation owner per surface. If native forking or that binding cannot safely deliver the assigned
slice, use the existing Tier B isolated implementation-worker route with its task-private writer,
or report the affected capability `UNKNOWN`; do not have an unbound Worker edit or invent a new
supervisor path.

These mechanics provide no inactive wake, background supervision, cadence, scheduler, durable
child-state store, or guaranteed opportunity to reply before a stop. Repository instructions are
also not proof that V2 supervision has been adopted: landing is inert for that reliance claim until
a fresh representative real Codex session passes the governing behavioral acceptance and any
separate process installation is authorized and read back. Unobserved host behavior stays
`UNKNOWN`.

An independent-review fork remains a distinct eligible reviewer. Root may track its identity,
eligibility, currentness, charter, liveness, and evidence delivery, but must not coach its verdict
or findings or replace it merely because the result is adverse. Findings continue through the
existing accept, challenge, evidence, and focused-rereview boundaries.

#### Setup and workflow failure journal

For feedback capture and routing, follow [feedback capture and triage](source-history-feedback.md#feedback-capture-and-triage)
and reuse the underlying issue. A local journal is execution evidence only when the exact assigned
work explicitly needs it; it is not a mandatory second report for every workflow failure.
When such a journal is required, use the repo-local, Git-ignored friction register.
`friction.md` is its short policy and category index; detailed entries
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

Stock Codex resume keeps the same session and mailbox binding. A fresh launch is independent: Marco directly names it `Codex Coordinator`, it takes over that mailbox, and the old owner is fenced. `/root` is only a temporary inbound alias. Registration or a later plain launch is not pickup of work; exact obligations still require an attributable handoff. The retained legacy Coordinator handoff controls exist only for already-running pre-cutover generations and are not a stock launch path.

Keep the handoff to four fields: **Git boundary** (starting record, final commit, and range), **trigger/change**, **outstanding work**, and **flag as broken** (the newly changed behavior whose failure the successor must report).

### Managed Codex worker

`scripts/switchstand --active <task> -- <assignment>` starts a one-task Worker. Trusted launch state injects the active WorkId, bounded read-only references, managed principal/currentness, and available tools. The Worker begins and re-enters with `work_get(api_version="1")` without inventing a WorkId. It may perform the bounded research/design/implementation/review function assigned to that work, and may write only the active work unless an explicit CURRENT grant says otherwise.

The managed surface is not general workspace discovery and is not interchangeable with the ordinary HTTP/OAuth surface. Exact-candidate isolated launch is a separate qualification route and does not itself activate or deploy anything.

### Ordinary ChatGPT

ChatGPT uses the authenticated repository-configured HTTP/OAuth MCP. It works with admitted provider-neutral WorkIds, durable messages, and protected operations. A chat without a repository uses `repository_bundle_get`, accepts only `current`, verifies the advertised SHA-256, and materializes the bundle as a normal repository. It retries `refresh_pending` and never substitutes stale cache. The default bundle contains only `main` with its original Git history. Pass `branch` for one exact different branch when needed; all-branch bundles and all-branch fallbacks are forbidden. `required_sha` must match the selected current branch and never selects additional branches. `review_bundle_get` likewise requires the exact candidate branch when it is not `main`. A chat with a normal checkout uses Git normally.

If `actions_artifact` is returned and an existing client tool supports GitHub Actions artifact download, use its exact run/name to download and extract the ZIP (full-history bundle, manifest, checksum), then verify the bundle and Git checkout. Do not assume such a tool exists or ask Marco to install one. Missing/expired artifacts are `unavailable`; missing transfer capability remains `MISSING_CAPABILITY` and client acceptance `NOT_RUN`. A locator or Release URL alone is not delivered repository content.

The returned resource link and metadata do not prove a usable ChatGPT checkout. Use an actually available binary download tool to save the linked bytes in the chat's execution environment, verify the advertised SHA-256, then use Git to clone the bundle and check out the selected branch or exact requested SHA. Read the guide from that verified checkout. Capture the actual tool and response when transfer fails; absent download or Git capability remains `MISSING_CAPABILITY` / `NOT_RUN`, without invented DNS or HTTP results. A host-side download or mocked MCP test does not establish actual ChatGPT acceptance.

When Marco must connect, reconnect, or reinstall Switchstand in ChatGPT, give the exact MCP endpoint on its own line with no trailing punctuation (currently `https://laptop.tail46f0b9.ts.net/switchstand/mcp`). Give the new chat exactly one trusted WorkId separately as its continuation anchor. Never use a Coordinator-local filesystem path as the ChatGPT-facing handoff mechanism: ordinary ChatGPT cannot read that path, and a list of WorkIds obscures which work actually grounds the session. The one WorkId must come from Marco's direct assignment or an explicit `CURRENT` grant; it neither expands authority nor replaces internal Coordinator handoff bookkeeping. Without one trusted WorkId, report `COVERAGE_GAP / UNKNOWN`.

For an unreleased candidate, use the separate endpoint and connection in the
[isolated real ChatGPT TEST MCP procedure](chatgpt-mcp-edge.md#isolated-real-chatgpt-test-mcp).
Check its current operational work first; the production endpoint above is not
the candidate-testing route.

The desired user model is one chat equals one stable named agent identity. The visible name, authenticated OAuth principal, hidden ChatGPT chat identity, MCP session generation, and any WorkId are distinct. The HTTP edge reads `openai/session` from MCP call metadata; the mailbox stores a hash of that value and binds one visible name to each principal-and-chat pair. Distinct chats under the same OAuth principal can register distinct names, while one chat cannot register multiple names. Repeating registration from an already-bound session returns its existing immutable name rather than creating another name or performing a takeover. The agent does not supply or see the hidden chat value. An unavailable chat identity yields a recovery result. Explicit same-principal takeover of an ordinary name fences the old chat. Every cross-principal takeover, including `Codex Coordinator`, first stops with `takeover_confirmation_required` unless the call carries Marco's full current message naming the target and directing the takeover. The response tells the agent that no takeover occurred, to ask Marco whether it was actually told to take over the exact name, and to retry with his full confirmation message. The server does not use a passcode and does not expose the internal principal mismatch as the public action. A confirmed Coordinator takeover renames the legacy `/root` row in place, preserves the endpoint and deliveries, replaces the principal/session binding, and advances generation. `/root` remains only an inbound lookup alias and every successful readback uses `Codex Coordinator`. The legacy `agent_transfer_request` and host approval path remains available for already-pending requests and recovery. This describes the code contract, not a claim that the live client journey has been accepted. Activation still requires app reinstall, a fresh chat, and exact schema/behavior verification.

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

**Tracker portfolio/handoff:** For an assigned multi-WorkId Tracker portfolio or named agent replacement, read the current Tracker guide `b974bf05-5397-53b5-ab5a-0932dbea1310` through authenticated MCP. It owns lightweight recovery, handoff and outstanding-obligation rules; it does not grant ownership or add a portfolio ceremony for narrow single-task workers.

Tracker Issues V1-A owns Feedback V1's shared reporting function and its code/tests; agents use it rather than adding another reporting path. The first-minute trigger and reply marks live in [AGENTS.md](../AGENTS.md); concrete-failure capture, rating and triage live in [source compatibility](source-history-feedback.md#feedback-capture-and-triage).

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

Codex and Claude sessions with a repository use Git; ordinary ChatGPT without a normal checkout
uses `repository_bundle_get` and never reconstructs the repository from stale cache. Stock Codex
has no implicit linked writer, launch manifest, or launcher state: inspect the checkout, use an
explicitly assigned writer or create one from shared `main`, and never synthesize missing launcher
state. Durable worktrees, clones, evidence, and handoff artifacts live under
`~/.local/state/switchstand`; reproducible caches live under `~/.cache/switchstand`; do not use
`/tmp` for durable work. Before any replacement write, reread the exact target and preserve user
changes. Use existing `~/.config/switchstand/.env` only when current authority permits the
corresponding capability; missing capability never justifies broader credentials or permissions.

Auxiliary Coordinator bookkeeping failure degrades only the smallest affected capability. Preserve
or quarantine meaningful malformed state where safe, repair or recreate it automatically when
possible, and continue unrelated diagnosis/edit/test work; do not make Marco the routine repair
operator. Missing consequential-effect authority disables only that effect.

Review and tests name their exact subject. Candidate review, exact-head checks, current-target composition, landing, deployment, and live reliance are different claims. Target advancement alone does not require a rebase: preserve the immutable reviewed candidate when composition can establish safety, and rebase only when needed or materially helpful. A rewritten head is a new exact candidate.

Use [development](development.md) for commands and evidence subjects, [code quality](code-quality.md) for implementation/review rules, and [architecture](architecture.md) for current owners. `AGENTS.md` remains the load-bearing bootstrap and router.
