# Open-tab multi-chat wake V1 — provisional review candidate

Work: 662bd6ec-ed4e-5ee7-b02a-34311a42d6ca. Current direct Marco direction,
relayed by parent: manual registration is acceptable; support multiple chats,
target real mailbox notices, show failures and qualify background operation.
Failure recipient subsequently answered **undecided for now**. No alerts may be
sent. No GUI manipulation. No production activation, migrations or credentials
changes. Original mock on18765 and its single event remain untouched.

Current source base: 0e08116569ec7e0c391ad562e976747d00525dd0 (PR1109).
Owned writer currently582b5dced plus untracked original prototype. Advance this
writer to that exact main before implementation; no canonical-main mutation.

## Existing capability and missing seam

`MessageState.all_pending_delivery_ids(binding)` collects fenced per-recipient
IDs using shared bounded reads; `pending_delivery(binding,id)` revalidates them.
`AgentMailboxState.by_name` resolves an actual registered endpoint/generation.
The existing local summary read in chatgpt_edge.py746 establishes loopback-only
metadata observation, but sender counts are not a delivery identity/dedupe key.
Registration remains MCP principal+hidden-session-bound. Human chooses the
mailbox/URL association explicitly; we do not claim browser UUID proves it.

No admitted reporter actor has been established. Never use the target mailbox
binding as a reporting sender or invent a service principal. Durable coordinator
dispatch is unavailable until recipient AND actual reporter authority exist.
This candidate must visibly show **escalation unconfigured** and cannot claim
coordinator notification. Parent knows this qualification.

## Smallest useful route

1. Add one opt-in loopback adapter to the candidate edge. Named poll resolves
   current registered mailbox and returns canonical name, endpoint, generation
   and pending delivery IDs only, using shared MessageState helpers. No payloads,
   receive/ack/disposition, mailbox registration or caller-selected principal.
   Unavailable/stale is an error, never empty. Existing generation is pinned in
   extension; replacement invalidates pairing rather than silently retargeting.
2. Manual popup: pair active exact existing chat URL/tab with named registered
   mailbox. Show the resolved mailbox and pairing; one mailbox/URL has one bound
   tab, multiple different chats supported. User confirms association. No bulk
   discovery, rename, create, registration automation or reopening closed tabs.
3. Keep per-binding seen/pending/attempt/status in chrome.storage.local, serialize
   checks. Coalesce still-pending delivery IDs; reserve those IDs before DOM
   mutation. Refresh server IDs before retrying definite pre-submit deferrals.
   Never retry submitted/uncertain IDs automatically, including after reload or
   re-pair. A new ID is a new notification, not permission to retry an unknown
   previous attempt. Unknown halts that binding for explicit reconciliation.
4. Preserve existing exact URL and empty composer checks; busy/draft defer;
   disarmed/STOP never sends. Frozen/discarded/missing/wrong-url tabs report
   disconnected/deferred without activating/focusing/navigating them. A click
   remains click-attempted, not provider acceptance or mailbox pickup.
5. Opt-in local report POST checks loopback + server-generated ephemeral bearer
   capability generated for this TEST runtime; no hardcoded production secret.
   It records a bounded structured error through existing server logging (no new
   table/store), with mailbox, event references, short status and timestamp, no
   draft/answer content. Its receipt means logged, not coordinator alerted.
   No report capability means report unavailable, shown visibly, no false success.
   Polling reports current connection and report availability; popup says last
   checked/time, unavailable/stale versus idle and escalation unconfigured.
   Server log survives through existing runtime supervisor journal where enabled;
   this is not a new durable alert queue or delivery guarantee.

Auth/trust: preserve existing local metadata trust boundary. Human selection is
association authority, not auth impersonation. The report capability permits
bounded log entries only, never messages/registration/work effects. A loopback config read supplies the random per-runtime report-only capability
to the extension; ordinary web origins receive no CORS access. Report requires
application/json and Authorization (preflight is not enabled). No user-facing
port/token fields, no generated credential file or copied real credential. No production env/OAuth copy. Exact token provisioning and isolated TEST
runtime readiness must be established before runtime qualification, not invented.

## Surfaces, decomposition and envelope

Proposed source: src/switchstand/chatgpt_wake.py (small adapter),
src/switchstand/chatgpt_edge.py (opt-in wiring), prototypes/chatgpt-wake/
worker.js,popup.js,popup.html,manifest.json,README.md; docs/architecture.md.
Support: tests/test_chatgpt_wake.py, prototype Node state/binding fixtures and
existing dependency-free browser fixture only as needed. Keep old mock separately
available and do not reinterpret its single untargeted event as real routing.

One behavior cut: multi-target polling + independent visible/logged failures.
Splitting just multi UI from real routing would misrepresent multi-chat support;
coordinator delivery is explicitly unavailable, not fabricated as a dependency.
Strongest smaller alternative: existing summary counts only. Reject for delivery
dedupe: same count can hide different pending messages; counts cannot safely
drive this adapter. Reuse ID observation instead, no shared engine rewrite.

Forecast delta ~120 backend/source+wiring, ~150 extension state/UI, ~180 focused
support plus retained original composer. These are alarms, not scope to spend.
No new DB tables, MCP tools, managed actors, services or permanent stores.
One opt-in runtime flag enables routes; per-runtime random capability is delivered
by loopback config read, no persistent credential config or OAuth architecture.
Unexpected actor/enrollment/persistence/monitor framework triggers replan.

## Behavioral contract and proof

Given two manually registered target chats with explicit pairings, a delivery to A
may nudge only A with exact literal `Process your mailbox`. B is untouched.
Busy/draft/STOP prevents insertion. Definite pre-submit failure is visible/logged;
uncertain send is visible and stops retries. Server actor movement invalidates
pairing. No pending IDs is idle, not acceptance/pickup of previous IDs.

Earliest falsifiers: isolated real PostgreSQL mailbox state verifies A/B IDs,
generation fencing, no message body exposure and no mutation on reads; local
HTTP boundary rejects nonloopback/missing report capability; actual log receipt
does not claim alert dispatch. Node extension fixture exercises two bindings,
same-count changed IDs, drafts/busy/unknown/reload and one event per intended tab.
Real headless isolated DOM composer fixture retained; no GUI. Final actual
background ChatGPT test is manual by Marco after readiness, exact identity and
one authorized targeted delivery. Minimized/frozen/closed behavior not proven
by a headless fixture. No production restart or current TEST takeover assumed.

Current capability: local8790 responds; historical TEST46770 unavailable. Existing
TEST Work9411 does not establish a present reusable candidate window. Isolated
runtime qualification remains a separate explicit current owner/authority step.

## Factual aggregate scope (no bundle cap)

Fixed base0e08116569ec7e0c391ad562e976747d00525dd0. Retained original
prototype472 lines:166 extension source,31 mock support,197 test support,78
README support. Original provisional delta forecast270 production/source plus
180 focused test support; aggregate forecast436 production+486 support=922
lines before deletions, architecture/design text additional support to measure.
The117-line provisional design is support, not executable product. This forecast
is an alarm prompting smaller reuse/deletion judgment, not an authorized budget
or hard cap. Rewrite obsolete single-binding fixture choreography rather than
add a parallel fixture world. Preserve useful real composer fixture.

Explicit counts:0 new tables,0 MCP tools,0 registered actors,0 deployed services,
0 additional processes required in ordinary use,0 new permanent stores. Reuse
existing chrome.storage.local with per-binding keys (one state ownership change);
1 opt-in edge runtime flag;3 local adapter handlers (config,poll,report);0 new
schedulers (reuse existing Chrome alarm). Existing server logging is the failure
sink; no new event store or heartbeat monitor. Changed binaries0 files/0 bytes.
Aggregate actual production/support/diff counts must be reported before review.
Any new actor/authenrollment/store/monitor or unexpectedly large support forces
replan; no numeric allowance authorizes scope growth. Parent explicitly rejected
a hard cap around this bundle under AGENTS.
