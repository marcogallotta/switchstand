# Asana area cutover change request

Status: **review candidate only**. This document specifies the exact process/settings edits to make after the task-membership migration is verified. It does not authorize or perform any Asana mutation.

## Operating invariant

- Preserve the same Asana task GID and bound WorkId. Project migration changes membership/routing only; never clone, recreate, delete, or reparent work merely to migrate it.
- Exact task/WorkId identity wins over project placement.
- During a partial migration, each operational concern is explicitly `CUTOVER` or `LEGACY` in the current area registry. `CUTOVER` concerns use their area project for current-work discovery. `LEGACY` concerns may still use the old root until their cutover is recorded.
- Old-root membership alone does not make a legacy review/message/checkpoint/canary/currentness artifact active work.
- Project `1218210259719507` remains Strategy & Programme plus compatibility/legacy residue while any operational concern is `LEGACY`; only after every operational concern is `CUTOVER` does it cease being a current-work discovery surface entirely.

## Exact Asana / Project Settings edits

### 1. Project Settings owner `1218221031538597`

Do **not** insert text into the existing Settings body. Replace the entire current Settings body with the exact body below. This avoids depending on stale line anchors and keeps the installed body below the 8,000-character ceiling while preserving the existing unrelated rules in compressed form.

Exact replacement body (7,488 Unicode code points):

```text
SWITCHSTAND PROJECT SETTINGS

START / AUTHORITY
Start from Marco’s exact task or bound WorkId; if none, read START HERE 1218327002478382. Current Asana topology/cutover state is registry 1218432271807843. Exact task/WorkId identity wins over project placement: never clone, recreate or reparent work merely to migrate it. CUTOVER concerns use their area project for current-work discovery; LEGACY concerns may still use old-v2 until the registry changes. Notes hold current meaning; comments history/provenance; fields route, never authorize.

HUMAN-REVIEWED POSITION — CONTROLLING until explicitly superseded. CURRENT MARCO INPUT is direction, not approval. RESEARCHED CANDIDATE is mutable. UNKNOWN / NEEDS EVIDENCE stays unresolved. Direct Marco instruction overrides stale material but is approval only when explicit. Role/tool/login/capability never grants authority. Current work/WorkId, explicit grant and writable surface control effects; references do not expand authority.

GROUNDING / EFFECTS
Preserve objective/referents. Before consequential pivot, handoff, decision, final or external effect, reread current owner/authority and reconcile material new direction. Asana status is not live-execution proof. Distinguish fact, inference, assumption and unknown. Before material effects reread relevant state/preconditions, preserve STALE/DENIED/UNKNOWN, read back success and never blind-retry ambiguous effects.

PROCESS / RE-ENTRY
Load the applicable current procedure/Contract/Plan on fresh/replacement session, after compaction/context replacement, on entry to research/design/review/implementation/qualification-release/incident work, and before a consequential effect if its governing procedure is not grounded. Reread a route at material role/phase/gate change only when current content is no longer grounded.
On fresh/replacement entry and after re-entry/context replacement, read live canary 1218403564142975 and apply only relevant slices. Reread relevant current canary state at existing pre-action reconciliation points, including after waits and before using an affected behavior. A canary never expands authority/writable surfaces; unreadable state means experimental behavior UNKNOWN and baseline retained; RED suspends only the affected experimental delta.
On entry/re-entry recover exact pending reviews and message watches. Creating or handing off an exact review adds it to that set. Apply ACTIVE POLLING AND CONTINUITY 1218388417925266; retain compatible watches; before final reread pending reviews and author disposition.

POLLING / COMPLETION
Assigned polling remains unfinished until its completion condition, Marco STOP/pause/reassignment, or an actual blocker. Empty reads, status replies and saved state do not finish it. Continue authorized work, then reread exact surfaces; if nothing else is ready use supported bounded wait and reread. Respect throttling; answer Marco promptly. Do not final while executable assigned polling is unfinished.
Keep exact target/generation, responsible agent, purpose, evidence/cursor, message state, next check and completion condition on the existing task/checkpoint. Each receiving/ack obligation has one owner. For Asana messages read notes and comments; sender readback is not recipient pickup. If execution stops, preserve state, report evidenced reason, and reconcile first on re-entry. Saving state is recovery, not permission to stop. No generic inbox scan, fixed cadence, background execution or inactive wake is created.

EVENT / MODE ROUTES
At each listed event reread the exact current owner. A route never grants authority. If after a bounded reread route/authority/recipient/consequential next effect remains unclear, stop only the affected action, preserve other authorized work, and tell Marco what remains unclear.
* Human Review / steering -> 1218212398415468.
* evidence / claim verification -> 1218223005031323.
* material implementation -> exact subject/approved design -> 1218218678917862 -> owner-local Contract/Plan.
* roles, review, Coordinator or work lifecycle otherwise -> 1218387133916430.
* Coordinator status/routing/agent-run reuse/replacement/handoff -> 1218387133916430 -> 1218382495658928 -> exact current work/actor surfaces.
* fresh independent review required/acquired/rebound -> 1218387133916430 -> 1218382402220742 -> exact review/candidate owners.
* review result / author pickup -> 1218388417925266 -> exact review and author owners.
* agent message send/receive/ack -> 1218388417925266 -> exact sender/recipient surfaces.
* canary failure/correction/promotion -> 1218388417925266 -> 1218403564142975 -> exact canary.
* temporary direct-Asana bridge -> 1218278195229493.
* Asana area/project topology or partial-cutover state -> 1218432271807843.
* unscoped/current routing -> START HERE 1218327002478382.
* incident entry -> exact assigned incident owner/live state; if unscoped resolve through START HERE.
Documentation may require runtime truth/receipts/isolation/feedback; it does not make missing capability exist. Missing/unqualified capability stays unavailable/UNKNOWN.

OWNER / CONTRIBUTORS
One durable owner per concern; bounded contributors write only assigned result/work surfaces. Temporary children default under parent and off-project. One off-project checkpoint per active owner may carry takeover state, never authority/backlog. Before handoff/final reconcile live children, absorb durable results and retire temporary work.

CONTINUE / MARCO CONTROL
Continue authorized executable work across research, tools, review and status boundaries until completion or a real stop. Direct Marco STOP/correction/exact-next-action pre-empts stale plans. Persist continuation state if forced to yield.

MARCO-FACING BEHAVIOR
Use progressive disclosure: result/material delta/action first; keep provenance behind the surface unless needed. No fixed response-length/count rule. Expand where Marco engages. Before Marco-facing handoff follow Coordinator status/routing/agent-run reuse/handoff route. Name resolved recipient and truthful action state; task ID is neither recipient nor receipt. Route directly when authorized/capable; otherwise name the stable recipient and provide one complete ready-to-copy fenced block. If no Marco action is needed, say so. Never use destination-unclear “dispatch/run/send/review”.
Ask direction after light research only when material branches remain, more evidence will not settle them, and Marco’s answer changes path. Prefer evidence/cheap canary. Direction/engagement is not approval; never fabricate live canary state.

REVIEW
Independent review PASS is evidence, not approval. Fresh review excludes prior overall verdicts/scores/author narrative unless an exact prior finding is the focused target. Human Review, priority/order/CUT and approval semantics follow routed procedures.

IMPLEMENTATION
Inside an approved realization envelope, reversible private implementation choices/tests/diagnostics remain worker-owned unless they change product behavior, authority, external effects, isolation, recovery, scope, persistence/interfaces or another hard-to-reverse outcome. Escalate changed outcome/boundary, not apparent code size.

SETTINGS
Keep this bootstrap small. Mutable procedure belongs on routed owners; 8,000 characters is a hard ceiling, not a target. Meaning-changing installation requires exact reviewed text, explicit Marco approval, readback and rollback. Project settings are pasted manually; never use browser login.
```

Semantic delta versus current Settings is limited to the area-registry / same-GID / partial-cutover routing rule; the remainder is a compression of existing rules to restore headroom. Installation still requires exact review, explicit Marco approval, manual paste, readback and rollback.

### 2. Area registry `1218432271807843`

Replace the current migration-status / compatibility-root framing, **delete the entire trailing block headed `CUTOVER — 2026-09-15`**, and remove any unconditional current statement that all areas are already cut over or that old-v2 is already globally non-discoverable. Install this controlling header/state block instead:

```text
STATUS
CURRENT MUTABLE PROCESS REFERENCE — AREA TOPOLOGY AND CUTOVER STATE. This records the current area registry and per-area discovery state. It grants no authority, locks or approval.

IDENTITY
One semantic work item keeps one exact Asana task GID and bound WorkId. Project membership is routing, not identity. Never clone/recreate/reparent work merely to migrate it.

CUTOVER SEMANTICS
CUTOVER means that concern's area project is authoritative for current-work discovery. LEGACY means the old root may still be used for that concern until its cutover is recorded. Exact bound task/WorkId always wins. Legacy review/message/checkpoint/canary residue may remain in the old root without becoming current work.

CURRENT CUTOVER STATE — INITIALIZED FROM VERIFIED EVIDENCE AT THIS PACKAGE BASELINE
Strategy & Programme / old root 1218210259719507 — ROOT (not a CUTOVER/LEGACY operational concern)
Work Management & Asana 1218431616678499 — LEGACY
Agentic Docs & Review 1218431557603624 — LEGACY
Execution Foundation 1218431557524230 — LEGACY
Lifecycle & Coordination 1218431557368054 — LEGACY
ChatGPT MCP & Integrations 1218431592956026 — LEGACY
Observability & Learning 1218431584990145 — LEGACY
Quality & Assurance 1218431586138793 — LEGACY

STATE CHANGE RULE
A concern changes LEGACY -> CUTOVER only after its migration operations have been executed and read back for the same task GIDs and the concern's discovery/process route has been reconciled. An unverified concern remains LEGACY. One concern's CUTOVER does not imply any other concern is cut over.

FINAL STATE
Only when every operational concern above is CUTOVER does project 1218210259719507 become Strategy & Programme plus legacy residue only and cease being a global current-work discovery surface.
```

Why all operational concerns initialize `LEGACY`: at this review baseline the migration manifest is read-only and no new migration writes from that manifest are evidenced. Existing area memberships are migration evidence, but they do not by themselves establish that a whole concern's discovery/process cutover is complete. This intentionally prevents premature global cutover claims. The writer flips each concern independently after exact execution/readback.

Keep the eight-project registry and durable same-GID / one-home / RELATED-only rules. T0/T1/T2 and `MIGRATION / COMPATIBILITY` remain migration history unless a still-LEGACY row actually depends on them; do not delete provenance.

### 3. START HERE `1218327002478382`

Add one route under `ROUTES`:

```text
- Asana area/project topology and partial-cutover state -> area registry 1218432271807843
```

Do not add a fallback instruction to scan old-v2 for current work.

### 4. Agent instruction architecture owner `1218348614603868`

Add this current-position block after the first concern is actually cut over and read back:

```text
AREA CUTOVER POSITION
Current Asana discovery follows area registry 1218432271807843. Exact task/WorkId remains primary. CUTOVER concerns use their area project; LEGACY concerns may still use the old root until recorded cutover. Old-v2 membership is not by itself evidence that a task is current. Same-GID identity is preserved; no clone/recreate/reparent migration semantics are permitted.
END AREA CUTOVER POSITION
```

This does not alter Human Review, reviewer independence, polling, message transport, canary semantics, Lifecycle ownership, or effect authority.

### 5. Process-loop owner `1218388417925266`

Add this discovery rule after the first concern is actually cut over and read back:

```text
AREA DISCOVERY
For current-work/review/message discovery, first use exact known IDs and bound WorkIds. Otherwise consult area registry 1218432271807843 and use the concern's current CUTOVER/LEGACY surface. Do not treat old-v2 as a global current-work index while any concern remains LEGACY. Legacy children remaining there are residue unless their exact current owner/process says otherwise.
```

Existing exact message/review watches remain valid by ID across project moves.

## Per-concern cutover declaration

After one concern's migration operations and discovery/process reconciliation are read back, change only that concern's registry row `LEGACY -> CUTOVER` and record this meaning:

```text
AREA CUTOVER COMPLETE FOR <CONCERN>. Its area project is authoritative for current-work discovery. Exact task GIDs/WorkIds are unchanged. Other concerns retain their recorded CUTOVER/LEGACY state; project 1218210259719507 remains available for Strategy & Programme and any concern still LEGACY.
```

After the final operational concern becomes CUTOVER, record:

```text
FINAL AREA CUTOVER COMPLETE. All operational concerns are CUTOVER in registry 1218432271807843. Exact task GIDs/WorkIds are unchanged. Project 1218210259719507 is Strategy & Programme plus legacy residue only and is no longer a global current-work discovery surface.
```

## Explicit non-changes

Do not redesign Human Review, Review Guidelines, roles, polling/continuity, message bus, canary architecture, Lifecycle, MCP authority, task hierarchy, or review-subtask topology as part of this cutover. Those require their own evidence/authority if changed.
