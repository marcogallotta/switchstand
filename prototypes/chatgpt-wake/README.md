# ChatGPT mailbox wake

Load this directory as an unpacked Chrome/Brave extension after starting the
existing Switchstand wake backend on `127.0.0.1:18766`. In an open ChatGPT
conversation, click the extension icon, enter its registered mailbox name and
click **Add chat**. The
extension verifies the mailbox and current tab in one action and rejects an
already paired mailbox or conversation. Saved chats appear alphabetically.

The extension polls about every 30 seconds and requires the backend's
`available_delivery_ids` (only mail not yet RECEIVED). A missing or malformed
field fails closed: no Stop or Send. Deploy the compatible backend poll contract
(PR #1284) before installing this extension. The legacy `delivery_ids` list
still includes RECEIVED mail for existing alert/recovery semantics.

For new AVAILABLE mail on an exact, armed, open paired ChatGPT conversation,
the extension may click one unique enabled **Stop** button to interrupt a busy
response, wait until the composer is idle, insert `Process your mailbox`, and
click Send after rechecking the same mailbox's AVAILABLE deliveries. A durable
per-delivery Stop fence prevents repeated interruption for the same mail even
if the browser click result is uncertain. Wrong conversations, closed tabs,
unrelated drafts, attachments and ambiguous controls are not modified.

Wake retries use a per-chat cooldown: after a Send attempt, no earlier than
2 minutes, then 4 minutes, then every 4 minutes while unreceived mail remains.
The persisted binding-level clock survives restart, replacement and new mail;
historical uncertain Send evidence is retained but does not permanently block
new AVAILABLE deliveries. A click does not prove ChatGPT accepted or processed
mail. **Resume** rechecks one chat; **Resume all** rechecks all saved chats.
**Remove** hides a chat and fences local actions without deleting backend mail.
Removed pairings can be replaced with fresh local binding state; stale popup
buttons cannot change the new binding. Old one-extra-Send and Re-arm-per-chat
workflows are retired. Re-arm all never clears recorded uncertainty.

Pending mail blocked by a closed tab is reported immediately to the local wake
backend. A persistent draft or any continuously pending mailbox is reported
after a minute of observation.
The popup shows whether an alert was confirmed. An uncertain report retains its
incident identity for replay. A confirmed blocked incident remains active while
any mail is pending; a fresh empty observation sends its recovery.

Focused checks:

```sh
node prototypes/chatgpt-wake/test-stop-busy.mjs
node prototypes/chatgpt-wake/test-minimal.mjs
node prototypes/chatgpt-wake/test-minimal-browser.mjs
```

The browser check uses an isolated temporary profile and intercepted ChatGPT
fixture. It does not exercise actual toolbar input or installed-extension reload,
and does not establish live ChatGPT delivery or mailbox pickup.


### Diagnosing a stalled wake

The backend journal (`journalctl --user -u chatgpt-wake.service`) records
`browser_wake observation` when a chat's observed state changes. Each receipt has
an observation timestamp, mailbox endpoint/generation, pending count (absent if
unavailable), exact blocker, uncertainty flag, and last browser action with its
own identity/timestamps/result. An older action is history, not a fresh attempt;
`click-attempted` does not prove ChatGPT accepted or processed the prompt.
`uncertain-wake` and `manual-reconciliation` are recorded diagnostic reasons,
not proof that a message was accepted or that automatic wakes stay fenced
forever. A prior ambiguous Send remains UNKNOWN in history; fresh validated
AVAILABLE mail can be retried after the persisted cooldown, while a possible
Stop click stays fenced for the same still-pending delivery.

`browser_wake incident` identifies the alert and phase; the matching
`browser_wake alert` records queued/stale/denied/unavailable results and reasons.
`browser_wake rejected` identifies the rejected route, reason and HTTP status.
Diagnostic reports never dispatch mailbox alerts. They contain no chat text,
draft text, URLs or tokens. Failed uploads are retried on the next check and
reported in the extension worker console. No backend receipt means there is no
backend evidence of that observation; it does not establish an empty mailbox.
