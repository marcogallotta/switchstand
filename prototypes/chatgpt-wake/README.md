# Open-tab ChatGPT mailbox wake V1

This candidate polls **real Switchstand mailbox delivery IDs** from the opt-in
local TEST edge on `127.0.0.1:18766`. It is separate from the previously loaded
single-event mock on18765. Loading this candidate does not start its backend.
The original candidate-stage proof was limited. Current qualification is recorded on
Work662bd6ec-ed4e-5ee7-b02a-34311a42d6ca: Marco confirmed the final Stop/Send
test with “it worked”; this is not stronger typed proof of each click.

## Manual pairing

Each ChatGPT conversation must first register its own mailbox through Switchstand
MCP. Registration uses that conversation's authenticated MCP session; the extension
does not register or impersonate it. With the candidate backend running:

1. Load unpacked this directory in Brave/Chrome's extensions manager.
2. Open the intended existing chat, open the extension, enter its exact registered
   mailbox name and choose **Verify mailbox**.
3. Check the resolved mailbox and exact URL, then **Pair and arm this chat**.
   Repeat for each different chat/mailbox. Keep those tabs open.
4. **Check now** checks all armed pairings. The regular alarm checks roughly every
   30 seconds. Only a paired mailbox with pending unseen delivery IDs can trigger
   `Process your mailbox` in its exact paired tab.
5. Read each chat name and plain status. **Stop** or **Stop all** suspends wakes
   while retaining pairing/history. **Remove** cancels queued wake dispatch and removes
   only that extension pairing, never its ChatGPT conversation, mailbox or backend audit.

The human confirms mailbox-to-chat association; a URL cannot prove the hidden MCP
session identity. One mailbox has one paired tab; another mailbox cannot reuse that
chat. A changed mailbox generation invalidates the pairing.

## Backend and failure boundary

`SWITCHSTAND_CHATGPT_BROWSER_WAKE=1` installs three loopback-only routes in the
candidate edge: `/internal/chatgpt-wake/config`, `/poll?name=…`, and `/report`.
The edge requires its existing database/service launch configuration. It must run
on owned loopback18766; do not reuse production configuration or an unrelated TEST
slot. The feature is off by default. No schema/bootstrap change is required.

Poll returns endpoint/generation/delivery IDs, never message bodies, and performs
no receive/ack/disposition. A per-runtime capability from config protects JSON
reports. Reports use existing server logging only: **escalation unconfigured**.
No coordinator message is sent; no legitimate reporting actor is configured.
Browser exit or machine sleep cannot reliably self-report; last-check time exposes
stale observation, without a new monitoring service.

Closed tabs retain their pairings and mailbox observation, report undeliverable
mail, and never reopen automatically. Drafts/busy/frozen/discarded/wrong-URL tabs
are deferred. Uncertain outcomes halt that binding, including across extension
reload; a new delivery cannot clear that halt. Reconcile the actual conversation
before any operator recovery. `click-attempted` is not provider acceptance or proof
that the chat processed its mailbox. A draft inserted without a send is retained.

## Focused evidence

```sh
node prototypes/chatgpt-wake/test-binding.mjs
node prototypes/chatgpt-wake/test-state.mjs
sh scripts/check tests/test_chatgpt_wake.py tests/test_mailbox_wake.py
```

`test-browser.mjs` additionally uses a temporary isolated headless Brave profile
and intercepted ChatGPT-origin fixture, with no npm dependencies or signed-in
profile. It tests the actual composer function, including delayed voice-to-send,
no-ID editable forms and exact URL guards. Run only with resource admission.
It does not prove real ChatGPT background operation. The earlier single-chat mock
was human-confirmed working; the original multi-chat candidate required an explicit live test. The current
Work records Marco’s final human PASS separately from typed dispatch evidence. The historical demo source is excluded from this delivery.


## Coordinator-alert add-on

The following design-stage bootstrap description is historical, not current activation
authority or runtime status. Current recipient is `browser extension`; exact configured
grant and blocked-incident receipts live on the owning Work. Current pending-mail
policy is described in [architecture](../../docs/architecture.md).

`ALERT-DESIGN.md` governs the optional reporter. Building this branch does not
change the running V1 service or create an identity. Live bootstrap and writable
reporter activation remain held until the exact reviewed manifest is approved.
The browser keeps drafts intact. Pending mail plus60 seconds of continuously
observed draft blocking, or a closed tab, qualifies one ChatGPT Wake Setup 2026-11-04 alert;
clear observation qualifies one recovery. STOP/disarm does not emit a recovery.
A worker restart or observation gap resets an unqualified draft timer. Fresh empty
mailbox observation recovers even while the tab stays closed. After an outage,
only an explicit host observation_expired result (committed replay proved absent
under its send lock) permits retiring that operation and requalifying fresh
evidence with a new UUID; ambiguity retains the old UUID and arguments. The sender persists
exact incident evidence; ambiguous requests keep the same identity and arguments.

The trusted host bootstrap commands are below. `plan` is local-file-only; the
other commands require the separately approved exact host effect and its existing
explicit DATABASE_URL capability. They never run Compose or migrations. Preserve
the generated manifest, its SHA and all result receipts; do not generate a new
identity to retry a partial operation.

```sh
PYTHONPATH=src .venv/bin/python scripts/chatgpt-wake-reporter plan --package EXACT_REVIEWED_COMMIT --manifest /owned/task/reporter.json
PYTHONPATH=src .venv/bin/python scripts/chatgpt-wake-reporter issue --manifest /owned/task/reporter.json --manifest-sha256 EXACT_SHA
PYTHONPATH=src .venv/bin/python scripts/chatgpt-wake-reporter check --manifest /owned/task/reporter.json --manifest-sha256 EXACT_SHA
PYTHONPATH=src .venv/bin/python scripts/chatgpt-wake-reporter revoke --manifest /owned/task/reporter.json --manifest-sha256 EXACT_SHA
```

The issuer admits only this Work, the fixed service/session and the message-only
grant. Exact existing manifest grants finish registration idempotently; mismatched,
revoked or advanced grants HOLD. Revocation affects only that exact reporter grant.
Host activation explicitly constructs `HostReporter(manifest, MessageState(writer,
GrantState(writer)))` and injects it into `install_browser_wake(server, reader,
reporter)` (or the edge's optional `browser_wake_reporter` parameter). `reader`
retains startup transaction-read-only enforcement; `writer` is a separately owned
engine. Neither constructor issues grants or registers agents. No actor credential
is returned to the extension; its existing report-only bearer and loopback/Host/
JSON/no-CORS guards remain.

Focused add-on qualification: `scripts/check tests/test_chatgpt_alerts.py
 tests/test_agent_messages.py tests/test_chatgpt_wake.py` plus
`node prototypes/chatgpt-wake/test-state.mjs`. Only disposable PostgreSQL is used.
Historical qualification required separate live activation. The owning Work now records
genuine blocked-incident delivery; the original Code Cleanup recovery watch remains open.

## Local extras (main landing HOLD)

`chatgpt-wake.service.in` is the bounded local user-service template. Render
`@WRITER@` and `@LAUNCHER@` only from the independently reviewed exact-head
activation package; verify with `systemd-analyze --user verify`. Install/start
only under this Work's current local activation grant. Marco subsequently authorized user-login startup: `[Install] WantedBy=default.target`,
without linger or administrator changes. [OPS.md](OPS.md) is the plain operating guide.
Three starts per minute bound permanent startup failures; transient failures restart
after three seconds. Health is `GET http://127.0.0.1:18766/internal/chatgpt-wake/config`:
require HTTP200, configured escalation, and recipient `browser extension`.
The launcher checks exact package HEAD, read-only observation and existing reporter
grant; it never issues/registers. The original manifest remains immutable issuance
provenance; current sends require the renamed parent's pinned endpoint/generation.
Stop the user service and restore the reviewed previous launcher to roll back.

Startup-note template — **OFF, unused until Marco decides** (documentation only):
`WorkId: <exact Tracker WorkId>; role: <Coordinator or Worker>.`
This template is not injected or automatic. Tracker owns assignments.
