# Local ChatGPT wake operations

This is the local test prototype for Work662bd6ec-ed4e-5ee7-b02a-34311a42d6ca.
PR1110 and PR1119 stay unmerged while Marco tests. The startup WorkId/role note
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
Alternatively, an existing draft blocking a send must be continuously observed
for 60 seconds; do not insert a draft just to manufacture proof. Busy, STOP,
disarmed, unavailable source and `attempt-outcome-unknown` are separate states.
Do not retry or overwrite the current Code Cleanup 3 UNKNOWN attempt/draft.
The candidate single-retry recovery applies only to newly witnessed no-click
attempts: exact owned text, unchanged pairing/pending IDs, no intervening edit,
and one consumed retry allowance. It never inserts again. Existing UNKNOWN,
clicked attempts and evidence lost on worker restart still require reconciliation;
matching composer text alone is insufficient. This candidate is not installed by
editing these docs or the backend unit.
The routing candidate follows the registered exact conversation URL across open
tabs, using one target per check. Away/back, another matching tab and close/reopen
need no re-pairing. It never opens or focuses a tab. A page-owned retry stays on
its original page; moving/reopening the chat cannot clear an unresolved halt.
A queued alert is not received until the parent actually receives it. A recovery
requires observed cleared pending IDs or a positively cleared former blocker.
The honest reporter TEST already received/acknowledged proves routing only;
genuine automatic incident delivery is still NOT_RUN until actually observed.

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
