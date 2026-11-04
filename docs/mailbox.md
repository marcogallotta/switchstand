# Mailbox maintenance

The mailbox stores durable requests and correlated results between agents. A committed delivery,
a wake notice, recipient pickup, and completed work are separate facts. This guide routes a fresh
maintainer to the code and tests; [messaging and source compatibility](source-history-feedback.md#durable-messaging-and-feedback)
owns agent procedure and authority. [ChatGPT MCP edge](chatgpt-mcp-edge.md) owns host configuration.

## Find the owner

Implementation lives in `src/switchstand/mailbox/`. The former root modules under
`src/switchstand/` retain public compatibility imports; change behavior in the package owner,
then check the existing callers through those imports. Edge and managed adapters remain outside
the package. Test paths below are under `tests/`.

| Change or investigation | Code owner | Existing evidence |
| --- | --- | --- |
| Delivery persistence, replay, receive/recover, ACK, correlated result and disposition | [`mailbox/messages.py`](../src/switchstand/mailbox/messages.py): `MessageState`; `_store`, `_disposition_result`, `*_admitted` transitions | `test_messages.py`; `test_agent_messages.py` for ordinary public behavior |
| Registered name, hidden session binding, generation fencing and takeover | [`mailbox/agent_mailboxes.py`](../src/switchstand/mailbox/agent_mailboxes.py): `AgentMailboxState`; host transfer command in [`mailbox/agent_mailbox_transfer.py`](../src/switchstand/mailbox/agent_mailbox_transfer.py) | `test_agent_mailboxes.py` |
| Ordinary `agent_message_*` tool admission and response shape | `chatgpt_mcp.py`: `agent_context`, `agent_message_*`; [`mailbox/agent_messages.py`](../src/switchstand/mailbox/agent_messages.py): public views | `test_agent_messages.py` |
| Managed WorkId-addressed `message_*` calls | `chatgpt.py`: `ChatGPTService`; `mcp.py`: managed tool adapters | `test_chatgpt_messages.py`, `test_managed_messages.py` |
| Committed delivery does not produce a Codex turn | [`mailbox/codex_inbound_wake.py`](../src/switchstand/mailbox/codex_inbound_wake.py): `InboundWake`; edge construction/lifespan in `chatgpt_edge.py` | `test_codex_inbound_wake.py` |
| Notice markers, typed-review hints or Claude summary rendering | [`mailbox/mailbox_wake.py`](../src/switchstand/mailbox/mailbox_wake.py): pure projections; `chatgpt_edge.py`: `/internal/claude-mail-summary`; `scripts/claude-mail-watch` | `test_mailbox_wake.py`, `test_claude_mail_watch.py` |

`wakeful.py` and the edge-monitor prototypes own a separate event/outbox contract, described in
[Wakeful](wakeful.md). The Codex mailbox wake adapter does not dispatch through that outbox.
Typed-review verdicts belong to the review service: ACK can dismiss their notification, but
ordinary result/disposition tools reject reserved review routes.

## Follow a request

| Operation | What success establishes |
| --- | --- |
| Send | Message and recipient delivery committed, or the exact prior identity/payload replayed. Neither pickup nor work completion follows. |
| Pending | Read-only page of AVAILABLE/RECEIVED deliveries, newest first. Ordinary pages contain at most ten; use `next_cursor` only for older pages, omit it for every fresh poll. |
| Receive | RECEIVED under current recipient/runtime fencing. A changed receiving binding requires explicit recovery of the same delivery. |
| Acknowledge | AVAILABLE/RECEIVED becomes ACKNOWLEDGED and leaves pending/attention. No reply, verdict, effect or request-watch closure is recorded. |
| Result reply | An ordinary correlated result is committed and the original RECEIVED/ACKNOWLEDGED request is dispositioned atomically. Exact replay converges both; a different reply identity conflicts. |
| Disposition | Exact evidence is validated and the delivery becomes DISPOSITIONED. Equal evidence replays; changed evidence conflicts. Business or provider success still requires its own evidence. |

Keep the original delivery and result identity when reconciling uncertainty. ACK is useful for a
processed notification, but open request watches still include ACKNOWLEDGED until disposition.
`mailbox.agent_messages.public_message` maps ACKNOWLEDGED to RECEIVED for the older public message shape;
that projection does not reopen a dismissed delivery.

Start validation with `test_agent_messages.py` cases
`test_acknowledge_batch_is_per_delivery_and_preserves_open_reply`,
`test_request_replay_watch_follows_current_delivery_state`, and
`test_result_and_disposition_roll_back_together`. For provider evidence and replacement currentness,
use `test_messages.py`; its tests exercise the database boundary, not just rendered responses.

## Trace a missing wake

1. `MessageState._store` commits before `_notify_delivery`; callback failure cannot undo mail.
   `review_transaction` publishes review deliveries only after the enclosing commit. Inspect the
   durable delivery and current binding before blaming notice rendering.
2. `InboundWake.start` baselines existing AVAILABLE mail as old, installs the callback, then
   `_watch` scans committed AVAILABLE deliveries once per second, including other-process sends.
   `signal` deduplicates discovery against observed delivery IDs and tracks fresh intent in memory.
3. `_run` checks the registered `agent_type`, resolves the recipient through configured daemon
   sockets, and serializes by actual Root. Roots must accept direct input; a direct Head routes
   through its owning Root while retaining its own mail processing. Nested actors are unsupported.
   Check route/actor/phase logs, configured control, binding generation and resource admission.
4. `_reconcile` subtracts markers found by `receipts` in exact Root user-input transcript pages.
   Busy turns return BUSY and retain fresh intent; daemon completion/status notifications trigger
   reconsideration. When idle, enabled, admitted and current, it submits `turn/start`. A marker
   proves notice injection, not receive, ACK, reply, disposition or completed work.

A restart loses in-memory freshness and baselines pending mail again. Old unseen mail therefore
remains durable but is not automatically redispatched (`OLDER_UNCERTAIN`). Ambiguous `turn/start`
acceptance suspends the recipient: preserve exact delivery/turn evidence and reconcile rather than
blindly retry. Definite pre-effect failures can be reconsidered on later signals. Disabling control
prevents future starts and cannot revoke a turn already accepted. Claude's watcher prints current
unacknowledged counts and exits; it never starts a turn or changes delivery state.

Use `test_codex_inbound_wake.py` cases `test_dispatch_boundaries`,
`test_committed_discovery_baselines_old_mail_and_deduplicates_callback`,
`test_receipts_ignore_nonuser_text_and_follow_pages`, and
`test_same_root_serializes_heads_and_attach_is_once`; use `test_claude_mail_watch.py` for the real
HTTP/subprocess watcher boundary. These tests establish repository behavior, not live activation.
Focused checks use the [development workflow](development.md#normal-local-workflow).
