# Local ChatGPT wake operations

This is the local test prototype for Work662bd6ec-ed4e-5ee7-b02a-34311a42d6ca.
PR1110 and PR1119 remain on HOLD pending Marco’s disposition. The startup WorkId/role note
is documentation only, OFF and unused. Tracker owns assignments.

The Chrome extension polls manually paired chats every 30 seconds. The local
`chatgpt-wake.service` listens only on `127.0.0.1:18766`, observes mailbox IDs
through a read-only database engine, and sends qualified incident/recovery alerts
through the existing admitted `ChatGPT Wake Reporter` to `browser extension`.
It checks draft presence locally; existing draft contents are never copied or
transmitted. It never automatically creates, renames or reopens chats.

## Check, start, stop and logs

Run these as Marco's normal user, without sudo:

```sh
systemctl --user status chatgpt-wake.service --no-pager
systemctl --user is-enabled chatgpt-wake.service
systemctl --user enable --now chatgpt-wake.service
curl --fail --silent http://127.0.0.1:18766/internal/chatgpt-wake/config
systemctl --user start chatgpt-wake.service
systemctl --user stop chatgpt-wake.service
systemctl --user restart chatgpt-wake.service
journalctl --user -u chatgpt-wake.service -n 40 --no-pager
journalctl --user -u chatgpt-wake.service -f
```

Healthy config is HTTP200, `status: ok`, `escalation: configured`, and
`recipient: browser extension`. Config also contains an ephemeral report token;
do not paste its full output into messages. Startup logs must say
`observation READ ONLY; loopback18766; no bootstrap effects`.
A healthy backend does not prove an individual ChatGPT wake was submitted.

The reviewed login-enabled unit starts with the user session's `default.target`;
no system service, linger or administrator change is involved. Automatic restart
waits three seconds, with at most three starts per minute. If it reaches its
start limit, first fix the logged cause, then run:

```sh
systemctl --user reset-failed chatgpt-wake.service
systemctl --user start chatgpt-wake.service
```

Database/container availability is required. The launcher checks exact writer
HEAD, unchanged issued manifest and existing reporter grant/parent identity.
A `HOLD` guard failure needs reconciliation of that exact package or binding;
do not edit the manifest, bypass guards, issue credentials or repeatedly restart.
The existing reporter grant expires 2026-11-11 at 14:59 UTC. After expiry alerts
are unavailable; no automatic renewal is installed. Route renewal through this
Work's owner, preserving the current identity and exact authority requirements.

## Pairing and a genuine test

Use the existing extension popup's Check now / Verify mailbox flow. Keep the exact intended
ChatGPT conversation open, use Verify mailbox, then Pair and arm this chat for its exact registered
mailbox. Existing pairings survive backend restarts; do not re-pair working chats.
Reload the extension only after an authorized extension-file change. Installing
this unit or editing this guide does not require an extension reload.

For an incident test, first verify an armed binding has **real pending mailbox
IDs**. Close that exact paired ChatGPT tab; its next normal poll can report one
closed-tab incident to `browser extension`. No extra source message is required.
If there are no genuine pending IDs, that test cannot yet prove an incident.
Any continuously observed pending mailbox qualifies after 60 seconds, including
HALT, wrong conversation, busy, or mail already seen after a click. An existing
draft retains its draft reason; other pending states use `pending-mail`. STOP,
disarming, source errors and observation gaps reset an unqualified timer.
Do not retry or overwrite the current Code Cleanup 3 UNKNOWN attempt/draft.
A queued alert is not received until the parent actually receives it. A recovery
requires a fresh matching observation with no pending IDs. STOP never emits recovery.
The initial reporter TEST proved routing only. The owning Work now records genuine
automatic blocked-incident delivery; the original Code Cleanup recovery watch remains
open and must not be confused with other incidents’ observed recoveries.

## “Failed to fetch” and rollback

Check service status, health and newest logs first. A stopped backend usually
recovers with Start; a rate-limited backend needs its cause fixed before reset.
A running service with a failing health URL needs diagnosis before relying on it.
Use the popup's Check again after health returns; do not clear Chrome storage,
re-pair every chat, reopen closed tabs or retry an UNKNOWN UI attempt.

To stop login startup and the backend reversibly:

```sh
systemctl --user disable --now chatgpt-wake.service
```

The rendered installed unit is `~/.config/systemd/user/chatgpt-wake.service`.
Exact launcher/activation hashes and live receipts are retained in
`/home/marco/.local/state/switchstand/chatgpt-wake-alerts-bootstrap/`.
Before restoring a prior package, stop this unit and have the owning agent select
its exact reviewed writer/launcher pair and verify HTTP200/read-only startup.
Never run two backends on18766 or change the reporter manifest/grant as rollback.
The preserved older de12ecf launcher restores observation but targets the former
parent name, so it does not prove current alert delivery. Extension rollback is
separate and must preserve existing pairings/storage/drafts.

## Diagnostic receipts and installed artifacts

The owning Work records authorized installed artifact hashes and runtime receipts;
installation alone does not prove a release was loaded or Remove was used live.
Existing service logs contain
`browser_wake diagnostic` records. Match attempt UUID and delivery IDs; release UUID maps to
exact artifact hashes in its activation proof. The envelope boot/release IDs describe the
executing logger; the attempt retains its original boot/release IDs across reloads.
A missing terminal result means only that no result was durably recorded; the DOM stage is
UNKNOWN. `legacy_halt: true` means no attempt receipt exists, not a new attempt/interruption.
Explicit Pair arms even with pending mail or a blocked composer and grants one fresh wake.
For an unresolved attempt it must retain the same endpoint/generation and conversation.
The previous attempt/HALT/IDs/consumed marker are archived, seen and incidents retained;
automatic checks never renew the grant or declare the previous outcome resolved.
`clickDispatched` records click invocation, never ChatGPT acceptance or mailbox processing.
Facts contain booleans/fixed reasons only, never drafts, URLs, labels or raw exceptions.
The latest failed diagnostic report is retried during ordinary Check; there is no queue or
history. A new attempt may replace an older unreported receipt. Logging does not itself repair HALT. The combined candidate retains conversation routing,
unique visible explicit Send selection (including exact case-insensitive aria-label Send),
with a bounded Stop/readiness phase, then a fresh worker STOP/mailbox fence and synchronous final Send.
Automatic exact-draft recovery requires durable known no-click and original pending identity;
an explicit manual Pair also permits one current exact wake draft without attachments.
Stop requires a unique visible enabled control and an empty or proven exact wake draft without attachments.
Timeout/error/restart after reservation stays HALTED; legacy UNKNOWN and prior-click HALTs
require explicit manual Pair for a fresh wake; arbitrary drafts remain untouched.
