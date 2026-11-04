# Shared mailbox wake observations — V1 design and execution plan

Status: provisional, design review required before implementation.
WorkId: `b72a785f-a459-51f2-8a01-0b2560021a8e`.
Immutable package base: `582b5dced4110e4b651365695136964aace1bb82`.
Owner: Worker `/root/research_chatgpt_control`; integration/review owner: parent `/root`.

## Outcome and authority

Marco asked to share code between Codex, Claude and a future ChatGPT extension, then explicitly directed: design the restructure, review until passing, implement, review, merge. This package restructures the two existing paths without changing their operating behavior. The extension prototype has a separate owner and writer. No extension implementation, new external endpoint, activation, deployment, migration or credential effect belongs here.

Baseline: #1106/#1107 implement default-off Codex daemon wake; #1108 implements a loopback-only read-only Claude summary and a polling script. Codex starts a turn. The Claude script reports mail and exits; it neither starts a turn nor acknowledges mail. The design preserves that difference. `BASELINE_PRESERVED` is the proposed classification, to be checked independently against this artifact and the existing source.

## Smallest useful shared boundary

Keep `MessageState` and `AgentMailboxState` as the sole durable mailbox/currentness owners. Introduce one small `mailbox_wake.py` module for read-only observation and pure notice projection. This is shared code used by the existing Codex adapter and Claude endpoint/watcher, not a new scheduling service.

1. A `MailboxWakeSource` wraps the existing `MessageState`. Its `pending_ids(binding)` follows the existing bounded ID pages and returns the complete tuple, or `None` if currentness fails. Its `attention(binding)` delegates to the existing fenced aggregate query. It never consumes, acknowledges, marks delivery, starts execution or acquires effect authority. Separate ID and aggregate projections avoid making Claude fetch every delivery merely to count mail.
2. Pure notice helpers own the existing delivery-ID marker encoding/decoding and the existing ordinary/review nudge formatting. They accept already-selected delivery metadata. Missing typed review identity remains an error; assistant text is never a receipt. The Codex adapter still decides which exact user inputs are eligible receipt evidence and follows transcript pages itself.
3. A small pure summary value/parser validates the existing `status`, count and sender shape and provides the existing human notice text. The Claude endpoint constructs that projection from `attention`; its script uses the same parser/formatter. Keep this module importable without database/web dependencies, using type-only imports for observation interfaces where necessary. The standalone script remains usable through its current invocation and needs no new environment setup.

The source exposes two intentional observations, not one universal eligibility rule: Claude reports all current unacknowledged mail, whereas Codex may wake only for fresh deliveries lacking an exact user-input receipt. Both observe the same durable mailbox and preserve unavailable/stale evidence. A future extension can consume the read-only source and the same receipt marker/notice contract; browser targeting, authentication and submission require their own implementation and proof.

## What stays in adapters

Codex retains its daemon connection, exact Root/Head resolution, direct-input gate, freshness set, per-Root lock, busy/idle event handling, STOP checks, resource admission, route tokens, turn start, receipt extraction from exact native user turns, and uncertain-send suspension. Move neither its scheduling loop nor its effect state into the common source. Preserve daemon input, outcome strings, default-off configuration, routing gate, freshness/backlog distinction and no-retry behavior.

Claude retains its loopback route/access checks, name lookup, HTTP/error status mapping, polling timing, proxy bypass and exit codes. The watcher uses the shared pure summary validation/formatting but does not become a background daemon or acquire a resume mechanism. Existing route and response shape remain compatible. No endpoint rename or compatibility alias is needed.

The single `MessageState.on_delivery` hook remains Codex-owned in this refactor. There is currently one push consumer, so a subscriber router would be speculative. A future extension push consumer must extend that seam without replacing Codex's callback; that is a separate reviewed change. Polling and future push may trigger observation, but neither substitutes for mailbox acknowledgment.

## Behavior and failure contract

Given a current binding, observation returns the existing mailbox projection without state mutation. Given a stale binding or unavailable source, it returns unavailable evidence rather than empty mail. Codex's fresh delivery IDs minus exact receipts remain eligible under its existing gates. Busy actors retain their dirty intent and are reconsidered on daemon completion/status events. A message arriving after an accepted wake remains a separate fresh event; moving formatting cannot change that behavior. An ambiguous `turn/start` remains UNKNOWN and is not retried. Claude returns a notice only for positive validated counts; malformed/unavailable responses remain errors. Receipt/nudge submission is not receipt, disposition or completion of the durable message.

The first discriminating proof is existing boundary tests still observing the same rendered Codex user input, gates and outcomes, plus the real HTTP/subprocess Claude watcher tests with unchanged output/exit semantics. Add focused coverage proving both adapters use the shared observation/projection and that stale/unavailable observations cannot appear empty. Retain real database tests for current bindings and unchanged mail state. No production service or live ChatGPT account is needed to prove a source-only refactor. Native-daemon execution is unchanged; existing tests and previously recorded PR proof are baseline evidence, not a claim that this candidate has been live-qualified.

## Implementation shape and evidence

One PR is the coherent cut: common owner plus both current consumers and their discriminating tests. A Codex-only helper extraction would be smaller but would not demonstrate shared code in the second current client. A universal wake coordinator would add unsupported Claude execution semantics and unnecessary scheduling state. Prefer deleting duplicated projection/validation from adapters over wrapping unchanged logic in forwarding classes; if the source wrapper proves empty ceremony, put the shared bounded observation in the existing `MessageState` owner and retain only pure helpers in the new module.

Writable package patterns: `src/switchstand/mailbox_wake.py`, `src/switchstand/codex_inbound_wake.py`, `src/switchstand/chatgpt_edge.py`, `src/switchstand/messages.py` only if the smaller existing-owner route is selected, `scripts/claude-mail-watch`, `tests/test_mailbox_wake.py`, the directly affected existing wake/edge/message test files, this artifact, and the narrow current architecture description. Support patterns: `tests/**`, `docs/**`. No unrelated file or prototype surface is included.

Original expected envelope: approximately 140–220 gross production/config lines and 100–180 support lines, mostly moving existing behavior. These are estimates/alarms, not a bundle size allowance; Marco's always-loaded WAR MACHINES instruction says never set a size cap around a bundle. New tables, tools, services, processes, flags, grant operations, persistent stores/state machines and runtime-config crossings: all zero. Binary allowance: zero files/bytes. Unexpected scheduling/persistence/trust/API machinery or a changed runtime/CLI contract triggers stop and a smaller-route review before expansion.

Sequence: independent raw-capability/gap review; compare this exact artifact and disposition findings; freeze reviewed design; implement the minimal shared owner and migrate both consumers; run focused checks; independent exact-head implementation review; required CI/current-target composition and merge through parent. No implementation before design PASS. A material human-owned tradeoff returns to Marco; routine realization within his unchanged restructure request does not add another permission gate.

Required commands, run in the owned writer after resource admission for test work:

```sh
sh scripts/check tests/test_mailbox_wake.py tests/test_codex_inbound_wake.py tests/test_claude_mail_watch.py tests/test_chatgpt_edge.py tests/test_messages.py
```

The worker-bound inspect/test/commit entry points remain the native writer boundary; parent owns any required additional qualified-check execution. Report exact candidate and command outcome; NOT_RUN remains NOT_RUN. Landing is source-only/default-off and does not establish deployment or activation. Rollback is reverting this one refactor PR; there is no schema/config migration.

Documentation projection: UPDATE the current architecture's wake ownership sentence and this design; preserve existing operational instructions because route/config/behavior are unchanged. No AGENTS, Main or Roadmap edits. Current-canary applicability: no new experimental runtime delta or activation in this package; existing resource admission and native wake gates remain required. Evidence outside this bounded scope stays UNKNOWN.
