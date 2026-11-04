# Open-tab ChatGPT mailbox wake V1

This candidate polls **real Switchstand mailbox delivery IDs** from the opt-in
local TEST edge on `127.0.0.1:18766`. It is separate from the previously loaded
single-event mock on18765. Loading this candidate does not start its backend.
No current live multi-chat/background result is claimed.

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
5. Read per-chat status and last-check time. **Stop this chat** or **Stop all**
   suspends wakes while retaining pairing and delivery history.

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
was human-confirmed working; this new multi-chat real-mailbox candidate still
requires the explicit manual live test. `mock-source.mjs` is retained solely for
the original demo and does not feed this candidate.
