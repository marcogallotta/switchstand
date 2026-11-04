# Route a registered conversation to one current open tab

Work 662bd6ec-ed4e-5ee7-b02a-34311a42d6ca; base efbd2ba574c8137520a54b134067a03f8f761835.
Authority: parent received/dispositioned exact claude-coordinator delivery
b29fdb4c-1207-5809-b979-7f76c6aad93a relaying Marco's order to fix brittle design.
Finding1 only: local conversation routing. Both PR1110/1119 landing HOLD.
Findings2–5, new permissions, auto-open/navigation/focus/history/UI automation,
reporter/backend changes and generic acceptance/seen redesign are excluded.

## Smallest mechanism

Reuse the existing binding's registered supported conversation URL as durable
identity. Keep exact URL equality; do not match titles, names, prefixes or new
URL aliases. Stored tabId is a preferred routing hint, not conversation identity.
No new registration format, table, event monitor or migration is necessary.

At each existing 30-second check, query open ChatGPT tabs and filter exact
registered URL matches. Select exactly one: the stored preferred tab if it still
matches, otherwise the lowest numeric tab ID. Do not inspect several composers
to choose an empty one. Existing busy/frozen/discarded/draft guards apply to that
single target. Do not open, move or focus tabs, or change the registered URL.
Existing host permissions already cover the query and script injection.

Mailbox endpoint/generation, pending/seen IDs, STOP, incidents and unresolved
attempts remain owned by the conversation binding across tab disappearance.
No matching tab means unavailable, using existing closed-tab incident semantics.
The onRemoved callback must resolve remaining matching tabs before declaring
absence; closing an obsolete hint or one duplicate is not conversation closure.
Retain the existing exact URL check inside nudge immediately before mutation.
A selected tab disappearing or navigating during dispatch never triggers an
immediate fallback send to another tab: preserve existing UNKNOWN/safe-no-action
semantics and let a later check resolve fresh routing where permitted.

Normal fresh attempts use the resolved target ID for dispatch. A no-click retry
records its actual page/tab ID independently of the preferred hint. It may run
only on that exact page/tab with its existing ownership token and guards; never
transfer it to a replacement tab. Missing page/witness forfeits that retry and
retains the unresolved halt. Legacy UNKNOWN stays halted even when a matching
tab reappears. New routing alone cannot authorize duplicate sends or erase a
halt. Unhalted bindings retain fresh pending mail and can wake in any selected
exact matching tab without re-pairing.

## Execution and proof

Writer: existing clean-bound send-recovery-owned writer; live backend writer
stays pinned at89e9752. Production worker.js only; existing state/browser fixtures
and README/OPS/docs architecture receive the minimum corresponding updates.
Design PASS precedes implementation. Independent exact code/activation PASS
precedes worker.js-only installation, with exact installed preimage/rollback.

Focused fixtures: same tab away/back; original away and matching different tab;
all matches closed then reopened; duplicate matches yield one click; close one
duplicate without false closure; selected tab navigation/removal race; unchanged
STOP/mailbox-gen/draft/seen fences; page-owned retry and legacy UNKNOWN do not
migrate. Use existing intercepted isolated browser fixture, no ChatGPT network.

Three actual live proofs are separate from fixtures: (1) Marco navigates away
and back in the same tab; (2) Marco opens the same registered chat in another
tab while the original does not match; (3) Marco closes every matching tab and
reopens the registered chat. Parent supplies one distinct authorized fresh
pending test identity per case and receives/dispositions mail. No real draft
contents are collected or cleared. Record actual human navigation, pending
identity and observed wake once; a click alone is not provider acceptance.
All three live proofs remain NOT_RUN until those human actions and observations.
