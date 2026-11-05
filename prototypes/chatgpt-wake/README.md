# Exact mailbox pairing

This first extension feature verifies one existing Switchstand mailbox and saves
its confirmed ChatGPT conversation. It does not insert text, stop a response, send
a wake, poll in the background, or send alerts.

Run the existing wake backend on localhost:18766, then load this directory as an
unpacked extension. Open an existing ChatGPT conversation, verify its registered
mailbox name, and confirm the displayed identity and conversation. Stop disables
the saved pairing without clearing its history or mailbox. Pending mail does not
prevent pairing. Unresolved attempts and existing different pairings are retained
and rejected rather than reset. Later feature PRs add manual wake dispatch and
background watching.

Run `node prototypes/chatgpt-wake/test-binding.mjs` for the pairing contract.
