# One retry after an extension-owned wake was never clicked

Work: 662bd6ec-ed4e-5ee7-b02a-34311a42d6ca. Base: 89e97528629f556d76cfe19a2825ec702a52cd39.
Marco reported one wake left in the composer following a ChatGPT error. Actual
acceptance of that event is UNKNOWN; no real browser state was inspected.
Parent accepted this bounded mechanism. Both PR1110/1119 remain unmerged HOLD.

## Evidence and smallest change

`nudge` positively knows it did not call `send.click()` when it returns
`inserted-not-submitted`. Today that leaves a permanent generic UNKNOWN halt.
After a click, neither unchanged composer text nor a provider error proves that
the prompt was not submitted. This slice recovers only the former case. It does
not infer acceptance from a vanished draft, DOM user-turn count, or a click.

Keep the existing nudge/checkBinding owner and 30-second alarm. Add an optional
attempt token to nudge, and a retry mode that never inserts or replaces text.
After inserting the fixed exact string `Process your mailbox`, retain a small
isolated-world witness: token, exact editor object and URL, invalidation flag.
Observe subsequent editor input/beforeinput/paste/change/keydown and DOM text
mutations, and form submission/button actions. Any observed edit/submission
action invalidates permanently, even if the text later returns. Install this
watch before the asynchronous send-control wait; do not invalidate on the
extension's own initial insertion. Page navigation/editor replacement loses
ownership. The witness contains no copied user draft text.

Only a returned structured result explicitly stating no click, exact owned text,
and still-valid token permits persistent `retry` evidence on the binding. Bind
that evidence to the exact URL/tab, mailbox endpoint/generation, attempt delivery
IDs, worker boot and current STOP version. Preserve the existing persistent halt
before any DOM mutation. Exceptions/missing results cannot create evidence.

On the next ordinary check, fresh metadata must retain the same endpoint/gen and
the exact pending attempt IDs; cancellation, new IDs, STOP/version change or a
new worker boot forfeits retry. Legacy UNKNOWN has no witness and remains fenced.
Existing closed-tab incident handling runs first. Persist consumption of the
single retry allowance BEFORE dispatching retry. Recheck STOP after that write.
Retry's DOM action is synchronous: check surviving token/editor/URL, exact text
(no trim-equivalent acceptance), absence of attachments/busy, and an enabled
non-voice send control; consume the page token before calling click. No waits or
reinsertion. An invalid/absent token or mismatch produces a terminal halted
status; there is no third attempt, automatic rearm, or new wake identity.

The normal first click also consumes its page witness before calling click.
Existing click-attempted seen-ID behavior remains unchanged; it is not upgraded
to an acceptance claim. A click exception stays UNKNOWN. Closed-tab and existing
draft escalation semantics remain unchanged. Retry exhaustion is visible in
Check now status/backend observation logs; it creates no new alert category.

## Files and focused proof

Production: `prototypes/chatgpt-wake/worker.js` only. Tests: existing
`test-state.mjs` and `test-browser.mjs`. Docs: this file, README.md and OPS.md.
No backend/unit/manifest/grant/pairing-schema migration, framework or dependency.
The live extras writer stays pinned at 89e9752; this new writer is independent.

State fixture: structured no-click receipt permits exactly one retry of identical
IDs; storage consumption precedes dispatch; restart/legacy UNKNOWN, STOP during
save, changed endpoint/gen/URL/tab/pending IDs and absent result never retry.
Retain existing closure/draft/recovery and STOP tests.

Isolated headless browser fixtures (no ChatGPT network): send control initially
absent then available yields one eventual click with exactly one insertion;
unchanged matching text without ownership cannot click; edit then restore,
replacement editor, attachment/busy/navigation or form submission invalidates;
first click followed by retained text cannot retry; disabled/missing control on
the one retry yields no further clicks. Include textarea and contenteditable.
Run only these focused JS checks with fresh memory admission for the browser.

Fresh independent design PASS precedes code. Fresh independent exact code and
installation-package PASS precedes any installed-byte change. No GUI, real
draft access, new source mail or test send. Live activation is separately
reconciled with current work/parent; backend remains on its unchanged launcher.
