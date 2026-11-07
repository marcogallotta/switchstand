# ChatGPT mailbox wake

Load this directory as an unpacked Chrome/Brave extension after starting the
existing Switchstand wake backend on `127.0.0.1:18766`. In an open ChatGPT
conversation, click the extension icon, enter its registered mailbox name and
click **Add chat**. The
extension verifies the mailbox and current tab in one action and rejects an
already paired mailbox or conversation. Saved chats appear alphabetically.

The extension polls pending delivery IDs about every 30 seconds. For an armed
matching conversation it may stop a response, insert `Process your mailbox`,
and click Send. It preserves an unrelated draft or attachment, and never opens
a closed tab. Definite no-send conditions can be retried on later polls.
An uncertain click remains fenced even after the mailbox becomes empty: an
empty poll cannot prove whether ChatGPT received or processed the wake. The
popup shows **Needs manual reconciliation** in that case. **Resume** rechecks
one chat; **Resume all** rechecks every saved chat. Neither clears an uncertain
send or alert. **Remove** stops local wakes and hides the chat after saving,
even offline or with pending mail/alerts. It retains backend mail and local safety history;
Add can replace a removed mailbox or conversation with fresh local bookkeeping.
Old popup buttons cannot affect the replacement. Backend mail/history stay intact. A click
is not proof that ChatGPT processed mail. Already-dispatched wakes cannot be recalled.
A failed Remove save is visible and fences this worker; restart may resume the saved chat.

In the popup, **Re-arm** asks for confirmation for one uncertain chat and
requires a fresh empty mailbox and resolved alert. **Re-arm all** immediately
clears uncertainty and arms all saved chats except removed chats or a pending
Remove. It preserves pairing, batch/seen IDs, action evidence and alerts, even
if mail or an alert remains. It only saves state; the next normal poll may wake
chats. An earlier uncertain Send may already have succeeded, so an explicit
re-arm can repeat a wake. It does not prove mailbox processing.


Closed chats remain paired and log a diagnostic observation without alerting anyone.
Reopening the same exact URL lets normal polling resume, subject to the existing
uncertainty fence. A persistent draft or continuously pending mail in an open chat
qualifies for an alert after a minute of observation; closed time does not count.
The popup retains historical alert confirmation, which does not establish current health. An uncertain report retains its
incident identity for replay. A confirmed blocked incident remains active while
any mail is pending; a fresh empty observation sends its recovery.

Focused checks:

```sh
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
`uncertain-wake` means automatic action is fenced; `manual-reconciliation`
means that fence remains even though mail is now empty.

`browser_wake incident` identifies the alert and phase; the matching
`browser_wake alert` records queued/stale/denied/unavailable results and reasons.
`browser_wake rejected` identifies the rejected route, reason and HTTP status.
Diagnostic reports never dispatch mailbox alerts. They contain no chat text,
draft text, URLs or tokens. Failed uploads are retried on the next check and
reported in the extension worker console. No backend receipt means there is no
backend evidence of that observation; it does not establish an empty mailbox.
