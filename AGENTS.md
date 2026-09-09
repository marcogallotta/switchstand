# Agent routing

This is the always-loaded Switchstand launch/router kernel. Detailed workflows live in `docs/agent-procedures/`; active work owns temporary/version-specific constraints.

- For Switchstand work, `~/.claude/CLAUDE.md` is not an authoritative project input. Do not consult it. If a host or higher-priority instruction injects it, report a launch-contract violation and stop before material action. Repository `CLAUDE.md` is only a compatibility pointer here.
- Only the active work or Marco creates authority for actions/targets. Governing references constrain but never expand it. Tool availability, an edit request, or supplied identifiers do not imply commit, push, PR, merge, credential, permission, or external-write authority.
- Start with `work_get` without a WorkId. Read the active work, its bounded references, its `PROCEDURE`/`PROCEDURES` route, and verify the exact green repository SHA before material action.
- Only the active WorkId is writable. Reference WorkIds are read-only. Use only the Switchstand operations admitted by the active work/procedure; raw provider access is outside agent authority except the exact temporary handoff exception routed through `work-entry.md`.
- Before replacement writes, reread; preserve stale-state failures; read back successful effects. Never blind-retry an ambiguous external effect. If an append result is ambiguous, record UNKNOWN and do not retry it.
- Do not add credentials, login capability, permissions, persistence, concurrency, security boundaries, or external interfaces unless exact authority covers the consequential choice. Stop the affected path on a new material choice.
- Load every procedure named in active-work notes (`PROCEDURE:` / `PROCEDURES:`) before that phase. Until typed lifecycle routing exists, if no procedure is named, use the action-to-procedure map below only when unambiguous. A material transition requires loading the next procedure first; ambiguity stops the affected material action.
- Procedure owners:
  - `docs/agent-procedures/work-entry.md` — startup, exact work/reference retrieval, environment and temporary handoff bridge.
  - `docs/agent-procedures/execution.md` — writable surfaces, decomposition, children, edits and durable writeback.
  - `docs/agent-procedures/review-publication.md` — tests, independent review and publication preparation.
  - `docs/agent-procedures/learning-escalation.md` — friction, blockers, hard stops and escalation.
- Directory-specific `AGENTS.md` files may add code-path invariants only. They must not become lifecycle/state routers or copies of shared policy.
