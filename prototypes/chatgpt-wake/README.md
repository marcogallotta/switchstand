# ChatGPT mailbox wake — operating guide

This is the current operator guide for the maintained Chrome/Brave MV3 extension. **Source remains `prototypes/chatgpt-wake/`** on reviewed repository main `d43d9cb9` (8 November 2026); moving it to `extensions/chatgpt-wake/` is planned, **not landed**. The backend is in `src/switchstand/chatgpt_wake.py` and its optional alert reporter in `src/switchstand/chatgpt_alerts.py`. Historical design documents are not installation instructions.

## Prerequisites and pairing

1. The existing Switchstand wake backend must be running on **loopback port 18766** with the browser-wake routes enabled; the extension alone does not start it. Confirm read-only service state with `systemctl --user is-active chatgpt-wake.service`.
2. Load the *reviewed installed directory* as an unpacked extension in Brave/Chrome, rather than pointing the browser at an arbitrary checkout. Preserve its current extension ID, profile and saved storage when updating; choosing a new load path can create a different extension identity. Repository commits, installed backing files and browser-loaded code are separate.
3. Open the **exact existing ChatGPT conversation** that should receive the wake. In the toolbar popup, enter its registered mailbox name and select **Add chat**. The worker checks the current tab, the mailbox's backend-resolved endpoint and generation, and whether the mailbox or conversation is already paired. A successful Add arms wake immediately; **do not use Add merely as a harmless diagnostic when mail may be pending**.
4. The backend resolves spelling, case and spacing aliases and returns the canonical name. The installed PR #1265 worker accepts a nonblank canonical response and saves that canonical name with endpoint/generation; it never guesses the backend normalization rule itself. **Live Add using an alias is still NOT_RUN**; backend alias resolution and the loaded worker were verified separately.

## Controls and what the statuses mean

| Control / display | Effect or operator interpretation |
| --- | --- |
| **Resume** / **Resume all** | Arm and immediately recheck one/all saved chats. May send a wake when pending; does not reset uncertain-send evidence or retry limits. |
| **Re-arm** | Offered for an uncertain chat. Requires fresh **empty** mail from the same endpoint/generation, validated old batch IDs and no unresolved alert. Fails closed otherwise. |
| **Re-arm all** | Arms saved rows but does **not** clear uncertainty, a send budget or alert history. It is not a retry bypass. |
| **Send one extra wake** | Offered after the three automatic reservations for that batch; once per batch, with the same cooldown and composer checks. The popup reports its result/refusal. |
| **Remove** | Saves a local removed row and stops future local checks/reports for it; hides the row. Does **not** delete backend messages, recall a dispatched wake, or erase backend history. |
| **Armed**, **Already nudged**, **Chat not open**, **Chat suspended** | Respectively ready/idle, pending IDs already seen, exact tab absent, or tab discarded/frozen. The worker never opens a closed tab. |
| **Wake outcome unknown**, **Needs manual reconciliation** | Automatic Send remains fenced. The latter also covers missing/invalid legacy batch evidence; do not clear storage to force a resend. |
| **Wake cooling down**, **Automatic wake limit reached** | Saved budget blocked a Send. Resume and bulk controls cannot override either condition. |

The worker checks armed chats approximately every **30 seconds** while Chrome permits alarm execution. It sends the fixed prompt `Process your mailbox` only in the bound **exact URL** with an idle, single usable Send control; it never clicks Stop or overwrites another draft or attachment. One pending batch allows **three automatic Send reservations**, at least **two minutes** between Send attempts, across worker restarts and entry points. Reservation is saved **before** browser execution; crashes or ambiguous results consume it, and only proven no-click outcomes refund it. Joining new delivery IDs does not reset a batch. An empty *same-binding* poll retires only a validated old batch whose IDs are all absent; this is **not proof of message processing**.

## Blocked alerts, recovery and safe removal

A missing bound tab with pending mail can raise a blocked alert immediately. Drafts or continuously pending mail qualify after about one minute. The extension saves incident identity before reporting; failed/ambiguous uploads retain that identity for replay. A confirmed blocked incident is recovered only after the mailbox is observed empty under the same binding. The popup distinguishes **Coordinator alerted**, **Alert not confirmed**, and **Recovery reported**. Receipt or reporter status does not mean the ChatGPT provider accepted a Send.

For a stalled chat, inspect pending count, tab state, draft/attachment, and the **last action's timestamp and outcome** before changing anything. `click-attempted` is uncertain provider acceptance, not a success signal. `busy`, `waiting` and `ready` are local composer observations. Do not delete/re-add merely to bypass an uncertain batch. Where mail has genuinely cleared and saved evidence is valid, use the offered **Re-arm**; otherwise reconcile the mailbox and alert before trying any further manual operation.

**Remove and re-add:** Remove saves a tombstone even if backend is offline; a later Add of the same mailbox/conversation can replace the removed pairing with a fresh local incarnation. This does not erase pending backend mail or preserve the former local retry accounting in the new row, so treat a re-add with pending mail as a *new possible Send* requiring explicit operator intent. Old popup actions cannot affect the replacement. If **Remove save fails**, the current worker fences further wakes but that safeguard is not guaranteed across restart; investigate durable state instead of assuming removal succeeded.

## Read-only diagnosis and installation evidence

Start with these **non-mutating** checks on the host:

```sh
systemctl --user is-active chatgpt-wake.service
journalctl --user -u chatgpt-wake.service --since "30 minutes ago" --no-pager
```

If the backend is available, this reports only canonical identity and pending *count* (not raw IDs or the report token); replace the mailbox spelling as needed:

```sh
python - <<'PY'
import json
from urllib.parse import urlencode
from urllib.request import urlopen

query = urlencode({"name": "ChatGPT Extension"})
with urlopen("http://127.0.0.1:18766/internal/chatgpt-wake/poll?" + query, timeout=3) as response:
    result = json.load(response)
print("mailbox:", result["name"], "endpoint:", result["endpoint"],
      "generation:", result["generation"], "pending_count:", len(result["delivery_ids"]))
PY
```

Correlate timestamps and canonical endpoint/generation across **poll**, `browser_wake observation`, `browser_wake incident`, `browser_wake alert` and `browser_wake rejected` records, the extension service-worker console, and actual mailbox **RECEIVED / ACKNOWLEDGED / DISPOSITIONED** records. The running backend **does not log every successful poll** and the worker deduplicates unchanged observations, so a quiet journal does not prove the extension stalled. Distinguish a browser click, a backend report, a mailbox pickup and provider acceptance; none implies the next. Do not print mail bodies, drafts, page URLs, credentials or the `/config` report token.

For installed-version checks, compare the reviewed **commit**, install receipt, unpacked backing/portal `worker.js` hashes, Brave extension ID and browser-loaded service-worker script **independently**; manifest version `0.3.0` alone is insufficient. **Last verified snapshot (8 November 2026):** Brave loaded PR #1265's `worker.js` (SHA-256 `ac22e3f92e06755b39da429701016310d8e20e01c8e4d7941883ebcf15d55284`); the backend service was running older `runtime-logging-afaa080063.py`. These are dated observations, **not proof of today's live bits**. Do not change the portal path, restart services, reload the extension or clear its saved bindings as part of read-only diagnosis.

## Focused checks and known limits

```sh
node prototypes/chatgpt-wake/test-minimal.mjs
node prototypes/chatgpt-wake/test-minimal-browser.mjs
```

The second command uses an isolated browser profile and intercepted fixtures, not live ChatGPT. Tests do not prove live provider delivery, installed reload or alias Add. **Still outstanding as of 8 November 2026:** the **50-ID alert** duplicates pending IDs in its wire envelope and incident, exceeding the backend **4096-byte** report limit (**HTTP 413**); its proposed fix has **not** landed. The separate **PR #1227** closed-tab reporting change remains **unmerged**; **PR #1228** diagnostics was **closed stale**, so its successful-poll/heartbeat logging is **not installed**. Keep these facts separate from the working retry budget, mailbox pickup and verified binary installation.
