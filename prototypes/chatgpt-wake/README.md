# Exact mailbox pairing and automatic wake

The extension verifies existing Switchstand mailboxes and saves each confirmed
ChatGPT conversation. Each exact conversation belongs to one mailbox identity. A Chrome alarm checks for new pending mail every 30 seconds;
Check now requests the same check manually. Either path attempts one wake per new
delivery-ID batch:
it may stop an active response, wait up to ten seconds for Send, and submit exactly
`Process your mailbox`. There is no alert delivery yet.

Run the existing wake backend on localhost:18766, then load this directory as an
unpacked extension. Open an existing ChatGPT conversation, verify its registered
mailbox name, and confirm the displayed identity and conversation. Stop disables
all saved pairings without clearing its history or mailbox. Pending mail does not
prevent pairing. Unresolved attempts and changed identities are retained
and rejected rather than reset. Pairing another mailbox preserves existing bindings.
Checks use the saved exact-URL tab if available, otherwise the lowest-ID exact match.
Closing a tab records its absence without opening a replacement; reopening the same
conversation restores routing. An internal endpoint Stop isolates that pairing; the
popup Stop still stops all pairings. Background checks defer while a draft is present or
observation fails; they never renew an uncertain attempt or clear Stop. Chrome may
delay alarms, so 30 seconds is a requested interval, not a delivery deadline.

Run `node prototypes/chatgpt-wake/test-binding.mjs` for pairing and dispatch state
contracts. The focused Playwright fixture exercises actual MV3 storage, messaging
and injected DOM execution in an isolated profile, including a real worker restart.
No test accesses an existing browser profile or sends external mail.

The worker durably reserves the original pending IDs before any Stop or insertion.
Stop cancels further dispatch across waits; fresh endpoint/generation and pending
identity are rechecked before synchronous final DOM checks and Send. A click is
not provider acceptance. An uncertain attempt stays blocked through Check, Pair
and worker restart; this version cannot rearm it. Other drafts and attachments
are preserved. Saved pairing history and backend mailbox records are not cleared.
