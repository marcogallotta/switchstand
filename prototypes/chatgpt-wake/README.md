# ChatGPT mailbox wake

Load this directory as an unpacked Chrome/Brave extension after starting the
existing Switchstand wake backend on `127.0.0.1:18766`. In an open ChatGPT
conversation, open `chrome-extension://<id>/popup.html`, enter its registered mailbox name and click **Add chat**. The
extension verifies the mailbox and current tab in one action and rejects an
already paired mailbox or conversation. Saved chats appear alphabetically.

The extension polls pending delivery IDs about every 30 seconds. For an armed
matching conversation it may stop a response, insert `Process your mailbox`,
and click Send. It preserves an unrelated draft or attachment, and never opens
a closed tab. Definite no-send conditions can be retried on later polls.
An uncertain click remains fenced even after the mailbox becomes empty: an
empty poll cannot prove whether ChatGPT received or processed the wake. The
setup page shows **Needs manual reconciliation** in that case. **Resume** rechecks
one chat; **Resume all** rechecks every saved chat. Neither clears an uncertain
send or alert. **Remove** requires a fresh empty mailbox observation and a
resolved alert. It hides the chat but retains its pairing and safety history;
the same mailbox or conversation cannot be added again automatically. A click
is not proof that ChatGPT processed mail.

Click the extension toolbar icon to **Re-arm all** saved, nonremoved chats.
There is no popup or confirmation. This clears wake uncertainty and arms each
chat even if mail or an alert remains. It preserves pairing, batch/seen IDs,
latest action evidence and alerts. The click saves state; the next normal poll
may wake chats. An earlier uncertain Send may already have succeeded, so an
explicit re-arm can cause a repeated wake. It does not prove mailbox processing.
The former setup page remains available at `chrome-extension://<id>/popup.html`
for adding bindings and inspecting their status; it is no longer the icon action.


Pending mail blocked by a closed tab is reported immediately to the local wake
backend. A persistent draft or any continuously pending mailbox is reported
after a minute of observation.
The setup page shows whether an alert was confirmed. An uncertain report retains its
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
