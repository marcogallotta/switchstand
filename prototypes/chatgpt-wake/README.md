# Exact mailbox pairing and manual wake

The extension verifies one existing Switchstand mailbox and saves its confirmed
ChatGPT conversation. Check now manually attempts one wake for new pending mail:
it may stop an active response, wait up to ten seconds for Send, and submit exactly
`Process your mailbox`. There is no background watching or alert delivery yet.

Run the existing wake backend on localhost:18766, then load this directory as an
unpacked extension. Open an existing ChatGPT conversation, verify its registered
mailbox name, and confirm the displayed identity and conversation. Stop disables
the saved pairing without clearing its history or mailbox. Pending mail does not
prevent pairing. Unresolved attempts and existing different pairings are retained
and rejected rather than reset. Later feature PRs add background watching.

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
